"""Local subprocess transport for the desktop controller. No automatic AI calls."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pipeline as p
from worker_adapters import ProviderRegistry


def pid_alive(pid):
    if not pid: return False
    if os.name=='nt':
        kernel=ctypes.windll.kernel32
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,int(pid))
        if not handle: return False
        try:
            code=ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally: kernel.CloseHandle(handle)
    try: os.kill(int(pid),0); return True
    except OSError: return False


class DesktopTransport:
    capabilities={'auto_retry':True,'pause_resume':True,'live_events':True,'autosave':True,'member_budgets':True,'parallel_members':True,'independent_pipelines':True}
    def __init__(self,root,secrets):
        self.root=Path(root)
        self.secrets=secrets
        self.lock=threading.RLock()
        self.processes={}
        self.preview_processes={}
        self.connections={}
        self.remember_request=None
        self.python=str(Path(sys.executable).with_name('python.exe')) if Path(sys.executable).with_name('python.exe').is_file() else sys.executable
        cache=self.root/'.control'/'studio-connections.json'
        if cache.is_file():
            try: self.connections=p.load(cache)
            except Exception: pass

    def _registry(self):
        c=p.config()
        path=Path(c.get('workers_file','workers.local.json'))
        if not path.is_absolute(): path=self.root/path
        return ProviderRegistry.from_config(p.load(path),include_defaults=False)

    def run_job(self,job_id,request,event_callback,cancel_event):
        job=p.job_path(job_id)
        with self.lock:
            if self.get_job_runtime(job_id).get('running'):
                raise RuntimeError('该任务已有后台工位运行，不能重复启动。')
            from studio_commerce import check_local_capacity
            check_local_capacity(self,job_id)
            if callable(self.remember_request):
                self.remember_request(job_id,request)
            from studio_controller_lifecycle import owner_identity
            payload={**request,'job_id':job_id,'secrets':self.secrets.snapshot(),'controller_owner':owner_identity()}
            args=[self.python,'-u',str(self.root/('studio_v2_runner.py' if (job/'pipeline.v2.json').is_file() else 'studio_runner.py'))]
            process=subprocess.Popen(args,cwd=str(self.root),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                encoding='utf-8',errors='replace',env={**os.environ,'PYTHONIOENCODING':'utf-8'},creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            self.processes[job_id]=process
            process.stdin.write(json.dumps(payload,ensure_ascii=False)+'\n'); process.stdin.close()
        final='waiting'
        for line in iter(process.stdout.readline,''):
            safe=self.secrets.redact(p.redacted(line))
            try: event=json.loads(safe)
            except ValueError: event={'type':'activity','text':safe.strip()[:1800],'level':'info'}
            if not isinstance(event,dict): continue
            if not event.get('text'): event['text']=event.get('activity','')
            if event.get('type')=='runner' and event.get('status'): final=event['status']
            event_callback(event)
        code=process.wait()
        with self.lock: self.processes.pop(job_id,None)
        if code and final=='waiting': final='needs_attention'
        self._notify(job_id,final)
        return {'job_id':job_id,'status':final,'exit_code':code}

    def request_pause(self,job_id,reason='Operator requested pause'):
        p.request_pause(job_id,reason)
        if not self.get_job_runtime(job_id).get('running'):
            job=p.job_path(job_id)
            if (job/'pipeline.v2.json').is_file():
                # Keep the team's attempts, candidates and accounting on an idle pause.
                from studio_v2_engine import V2Engine
                engine=V2Engine(self.root,job,p.load(job/'pipeline.v2.json'))
                previous=p.load(job/'checkpoint'/'metadata.json') if (job/'checkpoint'/'metadata.json').is_file() else {}
                engine.state['status']='PAUSED'
                current=engine.checkpoint(save_art=False,reason=reason)
                for member_id,entry in current['members'].items():
                    saved=previous.get('members',{}).get(member_id,{})
                    if saved.get('files')==entry['files'] and saved.get('blender_checkpoint',{}).get('saved'):
                        entry['blender_checkpoint']=saved['blender_checkpoint']
                engine.dump(job/'checkpoint'/'metadata.json',current)
                engine.version()
                return
            # No worker is active; files and SQLite states can be saved immediately.
            c=p.config()
            p.checkpoint_job(c,job,reason,state='PAUSED',save_art=False)
            import studio_snapshots
            studio_snapshots.save(p,job,'pause')

    def get_job_runtime(self,job_id):
        with self.lock:
            process=self.processes.get(job_id)
            if process and process.poll() is None:
                return {'running':True,'pid':process.pid,'status':'RUNNING'}
        path=p.job_path(job_id)/'.studio-runner.json'
        if not path.is_file(): return {'running':False}
        data=p.load(path)
        live=data.get('status')=='running' and pid_alive(data.get('pid'))
        pid=data.get('pid')
        confirmed_dead=False
        if data.get('status')=='running' and not live and isinstance(pid,int) and not isinstance(pid,bool) and pid>0:
            if process is not None and process.pid==pid and process.poll() is not None:
                confirmed_dead=True
            elif os.name=='nt':
                kernel=ctypes.windll.kernel32
                handle=kernel.OpenProcess(0x1000,False,pid)
                if handle:
                    try:
                        code=ctypes.c_ulong()
                        confirmed_dead=bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value!=259
                    finally: kernel.CloseHandle(handle)
                else:
                    # Access denied is not evidence that the owner exited.
                    confirmed_dead=kernel.GetLastError()==87
            else:
                try: os.kill(pid,0)
                except ProcessLookupError: confirmed_dead=True
                except PermissionError: pass
            if confirmed_dead:
                recovered=[]
                for name,row in p.stage_rows(job_id).items():
                    if row['status']=='RUNNING':
                        p.set_stage(job_id,name,'INTERRUPTED','Recorded owner exited; saved files, attempts and actual costs retained. Explicit recovery is required.')
                        recovered.append(name)
                data={**data,'status':'interrupted','owner_exit_detected_at':time.time(),'recovered_stages':recovered}
                p.dump(path,data)
                team=p.job_path(job_id)/'.team-state.json'
                if team.is_file():
                    saved=p.load(team)
                    for member in saved.get('members',{}).values():
                        if member.get('status') in ('RUNNING','CHECKING','CURATING'):
                            member.update(status='INTERRUPTED',error='所属运行器已退出，原产物、费用与次数保留。')
                    for stage in saved.get('stages',{}).values():
                        if stage.get('status') in ('RUNNING','CHECKING','CURATING'):
                            stage['status']='INTERRUPTED'
                    saved.update(status='INTERRUPTED',updated_at=time.time())
                    p.dump(team,saved)
        return {**data,'running':live,'status':str(data.get('status','unknown')).upper()}

    def get_connection_status(self):
        with self.lock: return dict(self.connections)

    def credentials_status(self):
        status=self.secrets.status()
        result=dict(status.get('providers',{}))
        for name in ('ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY','GAME_CLAUDE_KEY'):
            if os.environ.get(name): result.setdefault('claude-code',{'configured':True,'source':'现有本机环境'})
        if os.environ.get('GAME_RESEARCH_KEY'): result.setdefault('openrouter-research',{'configured':True,'source':'现有本机环境'})
        return {'providers':result,'load_error':status.get('load_error',False)}

    get_credentials_status=credentials_status

    def check_connections(self,provider_ids):
        registry=self._registry(); config=p.config(); result={}
        for provider_id in provider_ids:
            spec=registry.get_provider(provider_id)
            row={'checked_at':time.time(),'model_tested':False}
            try:
                if spec.adapter=='codex':
                    import adapter_runtime
                    p.check_auth(adapter_runtime.native_config(p,config,spec))
                    row.update(status='READY_LOCAL',detail='Codex 程序及 ChatGPT 订阅登录可用；未调用模型。')
                elif spec.adapter=='claude':
                    import adapter_runtime
                    private=self.secrets.snapshot()
                    if 'claude-code' not in private:
                        existing=next((os.environ.get(name) for name in ('GAME_CLAUDE_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY') if os.environ.get(name)),None)
                        if existing: private['claude-code']=existing
                    native=adapter_runtime.native_config(p,{**config,'_provider_secrets':private},spec)
                    args=p.command_for('claude',native)+['--version']
                    completed=subprocess.run(args,capture_output=True,encoding='utf-8',errors='replace',timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    if completed.returncode: raise RuntimeError('Claude Code 本机程序无法运行。')
                    row.update(status='CONFIGURED',detail='Claude Code 与密钥已配置。当前检查未发 API 请求；可明确批准 0.20 美元连接实测。')
                elif spec.adapter=='manual-desktop':
                    row.update(status='AVAILABLE',detail='任务包交接可用；等待返回文件时不调用模型。')
                elif spec.adapter=='kimi':
                    from studio_kimi_adapter import command_for,clean_env
                    args=command_for(spec,config)+['--version']
                    completed=subprocess.run(args,capture_output=True,encoding='utf8',errors='replace',timeout=15,
                        env=clean_env(os.environ,self.root/'.control/kimi-local-check'),creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    if completed.returncode:raise RuntimeError('Kimi Code 本机CLI无法运行。')
                    row.update(status='READY_LOCAL',detail='Kimi 原生CLI本机版本可用；会员OAuth、实际模型和MCP任务尚未实测，未启动模型。',version=completed.stdout.strip()[:40],authentication_verified=False)
                elif spec.adapter=='external-command':
                    p.command_for('external_adapter',{'external_adapter_command':list(spec.command)})
                    row.update(status='CONFIGURED',detail='外部交接程序已配置，需其自己的登录或 API 接入。')
                else:
                    configured=provider_id in self.secrets.snapshot() or bool(os.environ.get('GAME_RESEARCH_KEY'))
                    row.update(status='CONFIGURED' if configured else 'OFFLINE',detail='研究模型已配置；API 实测须另行批准。' if configured else '需要本机研究 API 凭据。')
            except Exception as error:
                row.update(status='ERROR',detail=self.secrets.redact(p.redacted(str(error))))
            result[provider_id]=row
        with self.lock:
            self.connections.update(result)
            p.dump(self.root/'.control'/'studio-connections.json',self.connections)
        return {'connections':result,'additional_model_calls':0}

    def test_connections(self,provider_ids,limit_usd=.2):
        if len(provider_ids)!=1:
            raise RuntimeError('为保持总测试阈值，请每次选择一个 AI 进行 API 连接实测。')
        if float(limit_usd)>.2 or float(limit_usd)<=0:
            raise RuntimeError('连接实测预算限定为最多 0.20 美元。')
        spec=self._registry().get_provider(provider_ids[0])
        if spec.adapter!='claude':
            raise RuntimeError('此连接实测入口用于 Claude Code；其他接入使用其结构化工位测试。')
        # A separate child keeps the approval and secret out of this web process's global pipeline state.
        payload={'action':'check-model','provider_id':provider_ids[0],'limit_usd':float(limit_usd),'secrets':self.secrets.snapshot()}
        completed=subprocess.run([self.python,str(self.root/'studio_connection_test.py')],input=json.dumps(payload),
            capture_output=True,encoding='utf-8',errors='replace',cwd=str(self.root),timeout=240,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        safe=self.secrets.redact(completed.stdout)
        if completed.returncode: raise RuntimeError(safe[-1200:] or '连接实测失败；没有自动重试。')
        result=json.loads(safe)
        with self.lock:
            self.connections.update(result['connections'])
            p.dump(self.root/'.control'/'studio-connections.json',self.connections)
        return result

    def get_budget(self,job_id):
        job=p.job_path(job_id); path=job/'.adapter-state'/'budget.json'
        plan_path=job/'pipeline.v2.json'
        if plan_path.is_file():
            plan=p.load(plan_path)
            from studio_v2_budget import TeamBudgetLedger
            caps={role:spec['budget_usd'] for role,spec in plan['roles'].items()}
            members={member['id']:{'role':role,'budget_usd':member['budget_usd']} for role,spec in plan['roles'].items() for member in spec['members']}
            team_path=job/'.team-budget.json'
            if team_path.is_file():
                ledger=TeamBudgetLedger(team_path,str(plan['total_budget_usd']),caps,members)
                data=p.load(team_path)
                active=[name for name,entry in data.get('invocations',{}).items() if entry.get('state')=='pending'] if self.get_job_runtime(job_id).get('running') else None
                snapshot=ledger.snapshot(active_invocations=active)
                return {**snapshot,'cap_usd':snapshot['total_cap_usd'],'job_cap_usd':snapshot['total_cap_usd'],'source':'本流水线累计报告费用，含各工位与成员'}
            return {'total_cap_usd':plan['total_budget_usd'],'cap_usd':plan['total_budget_usd'],'job_cap_usd':plan['total_budget_usd'],
                'spent_usd':0,'reserved_usd':0,'remaining_usd':plan['total_budget_usd'],'available_usd':plan['total_budget_usd'],
                'costs_complete':True,'blocked':False,'invocations':{},
                'roles':{role:{'cap_usd':cap,'total_cap_usd':cap,'spent_usd':0,'reserved_usd':0,'remaining_usd':cap} for role,cap in caps.items()},
                'members':{name:{'role':spec['role'],'cap_usd':spec['budget_usd'],'total_cap_usd':spec['budget_usd'],'spent_usd':0,'reserved_usd':0,'remaining_usd':spec['budget_usd']} for name,spec in members.items()},
                'source':'尚未发生收费调用'}
        task=p.load(job/'task.json')
        cap=min(float(p.config().get('worker_budget_usd_per_job',3)),float(task.get('budget_usd',p.config().get('worker_budget_usd_per_job',3))))
        if path.is_file():
            data=p.load(path); cap=float(data['total_cap_usd'])
            entries=list(data['invocations'].values())
            known=sum(float(entry['cost_usd']) for entry in entries if entry.get('state')=='reconciled')
            reserved=sum(float(entry['reserved_cap_usd']) for entry in entries if entry.get('state')=='pending')
            unknown=any(entry.get('state')=='unknown' for entry in entries)
            unknown_ids=[name for name,entry in data['invocations'].items() if entry.get('state')=='unknown']
            interrupted=[name for name,entry in data['invocations'].items() if entry.get('state')=='pending' and not self.get_job_runtime(job_id).get('running')]
            return {'cap_usd':cap,'job_cap_usd':cap,'spent_usd':known,'reserved_usd':reserved,'remaining_usd':max(0,cap-known-reserved),
                    'invocations':data['invocations'],'unknown_invocations':unknown_ids,'interrupted_invocations':interrupted,
                    'costs_complete':not unknown and reserved==0,'blocked':unknown or bool(interrupted) or known+reserved>=cap,'source':'供应商 / CLI 报告估算'}
        report=job/'delivery'/'cost_report.json'
        known=p.load(report).get('production_reported_cost',0) if report.is_file() else 0
        return {'cap_usd':cap,'job_cap_usd':cap,'spent_usd':known,'remaining_usd':max(0,cap-float(known)),
                'costs_complete':True,'source':'已保存的制作费用估算' if report.is_file() else '尚未发生付费调用'}

    def open_target(self,job_id,target):
        job=p.job_path(job_id)
        if target=='play':
            if p.load(job/'task.json').get('kind')=='software':
                import socket
                import webbrowser
                manifest=p.load(job/'game'/'app-manifest.json')
                entry=p.within(job/'game',manifest['entrypoint'])
                config=p.config(); env=p.env_for('none',config)
                if manifest['runtime']=='html':
                    with socket.socket() as free:
                        free.bind(('127.0.0.1',0)); port=free.getsockname()[1]
                    command=[self.python,'-m','http.server',str(port),'--bind','127.0.0.1','--directory',str(job/'game')]
                    process=subprocess.Popen(command,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    self.preview_processes[job_id]=process
                    webbrowser.open('http://127.0.0.1:'+str(port)+'/'+entry.relative_to(job/'game').as_posix())
                else:
                    command=[self.python,str(entry)] if manifest['runtime']=='python' else p.executable('node')+[str(entry)]
                    self.preview_processes[job_id]=subprocess.Popen(command,cwd=str(job/'game'),env=env,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
                return {'opened':'software','entrypoint':str(entry)}
            subprocess.Popen([p.config()['godot_path'],'--path',str(job/'game')],cwd=str(job/'game'),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
            return {'opened':'game'}
        choices={'delivery':job/'delivery','project':job/'game','preview':job/'delivery'/'preview.gif','checkpoint':job/'checkpoint'}
        path=choices.get(target)
        if path is None or not path.exists(): raise RuntimeError('该结果尚未生成；请查看真实任务状态。')
        p.ensure_plain_path(path)
        if os.name=='nt': os.startfile(str(path))
        return {'opened':str(path)}

    def _notify(self,job_id,status):
        config=p.config()
        if not config.get('studio_policy',{}).get('notify',False): return
        script=self.root/'studio_notify.ps1'
        if os.name=='nt' and script.is_file():
            message='任务 '+job_id+(' 已完成，可查看交付。' if status=='completed' else ' 进度已保存，需要处理或恢复。')
            subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),'-Message',message],
                creationflags=subprocess.CREATE_NO_WINDOW,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
