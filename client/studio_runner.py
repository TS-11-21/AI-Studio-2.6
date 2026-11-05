"""One job per private subprocess, with live public activity and checkpoints."""
from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pipeline as p
import adapter_runtime as adapters
import studio_blender
import studio_snapshots

_EVENT_LOCK=threading.RLock()
_JOB=None
_STDOUT_AVAILABLE=True


def prepare_shared_schemas(config):
    """Shared schemas need no engine, native CLI or MCP connection."""
    p.CONTROL.mkdir(parents=True,exist_ok=True)
    p.dump(p.CONTROL/'empty.mcp.json',{'mcpServers':{}})
    p.dump(p.CONTROL/'result.schema.json',p.SCHEMA)
    p.dump(p.CONTROL/'research.schema.json',p.RESEARCH_SCHEMA)


def emit(event):
    global _STDOUT_AVAILABLE
    event={**event,'job_id':_JOB.name if _JOB else event.get('job_id'),'at':time.time()}
    event.setdefault('text',event.get('activity',''))
    content=p.redacted(json.dumps(event,ensure_ascii=False,default=str))
    with _EVENT_LOCK:
        if _JOB:
            with (_JOB/'.studio-events.jsonl').open('a',encoding='utf-8') as output:
                output.write(content+'\n')
        if _STDOUT_AVAILABLE:
            try:
                print(content,flush=True)
            except (BrokenPipeError,OSError):
                # The controller can close while this owned worker continues.
                # File events/results remain authoritative; losing telemetry
                # must not stop stdout readers or invalidate the handoff.
                _STDOUT_AVAILABLE=False
                # Avoid a second failed flush during interpreter shutdown.
                with contextlib.suppress(OSError,ValueError,AttributeError):
                    with open(os.devnull,'w',encoding='utf-8') as sink:
                        os.dup2(sink.fileno(),sys.stdout.fileno())


def public_activity(line,stage):
    """Show actions and visible answers, never private reasoning blocks."""
    try:
        event=json.loads(line)
    except ValueError:
        return {'type':'activity','stage':stage,'activity':line.strip()[:500],'text':line.strip()[:1200],'level':'info'} if line.strip() else None
    kind=event.get('type','')
    item=event.get('item',{})
    if item.get('type')=='reasoning' or kind in ('reasoning','thinking'):
        return None
    if kind in ('item.started','item.completed'):
        item_type=item.get('type')
        if item_type=='command_execution':
            return {'type':'activity','stage':stage,'activity':('正在执行：' if kind=='item.started' else '执行结束：')+str(item.get('command',''))[:260],
                    'text':str(item.get('aggregated_output',''))[-1200:],'level':'error' if item.get('exit_code') not in (None,0) else 'info'}
        if item_type=='mcp_tool_call':
            return {'type':'activity','stage':stage,'activity':('正在调用工具：' if kind=='item.started' else '工具已返回：')+str(item.get('server',''))+'/'+str(item.get('tool','')),'level':'info'}
        if item_type=='file_change':
            paths=[change.get('path','') for change in item.get('changes',[])]
            return {'type':'activity','stage':stage,'activity':'已更新文件：'+', '.join(paths)[:500],'level':'info'}
        if item_type in ('agent_message','assistant_message'):
            return {'type':'activity','stage':stage,'activity':'AI 返回工作说明','text':str(item.get('text',''))[:1800],'level':'info'}
    if kind in ('assistant','user'):
        visible=[]
        blocks=event.get('message',{}).get('content',[])
        if not isinstance(blocks,list):
            return None
        for block in blocks:
            if block.get('type')=='tool_use':
                name=str(block.get('name',''))
                data=block.get('input',{})
                path=data.get('file_path') or data.get('path') or data.get('filepath')
                label='正在调用工具：'+name
                if path: label+=' · '+str(path)[:200]
                visible.append(label)
            elif block.get('type')=='tool_result':
                visible.append('工具执行失败' if block.get('is_error') else '工具执行已返回')
            elif block.get('type')=='text':
                visible.append(str(block.get('text',''))[:1600])
        if visible:
            return {'type':'activity','stage':stage,'activity':visible[0][:500],'text':'\n'.join(visible)[:1800],'level':'info'}
    if kind=='result':
        return {'type':'activity','stage':stage,'activity':'AI 调用结束：'+str(event.get('subtype','complete')),
                'cost_usd':event.get('total_cost_usd'),'level':'error' if event.get('is_error') else 'info'}
    if kind in ('error','turn.failed'):
        return {'type':'activity','stage':stage,'activity':'执行错误','text':str(event.get('message') or event.get('error') or event)[:1400],'level':'error'}
    return None


def live_process(args,cwd,env,*,prompt='',timeout=120,log=None):
    from studio_controller_lifecycle import check_dispatch
    check_dispatch()
    if p.STOP.is_set():
        raise RuntimeError('Paused; no new process is launched.')
    p.ensure_plain_path(Path(cwd))
    if log: p.ensure_plain_path(log)
    stage=next((name for name in adapters.STAGE_ROLES if log and log.name.startswith(name)), 'qa' if log and 'godot_' in log.name else 'environment')
    process=subprocess.Popen(args,cwd=str(cwd),env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    with p.CHILD_LOCK: p.CHILDREN.add(process)
    captured={'out':[],'err':[]}
    handle=None
    if log:
        log.parent.mkdir(parents=True,exist_ok=True)
        handle=log.open('w',encoding='utf-8')
    lock=threading.RLock()
    def read(stream,key):
        for line in iter(stream.readline,''):
            safe=p.redacted(line)
            with lock:
                captured[key].append(safe)
                if handle:
                    handle.write(safe); handle.flush()
            activity=public_activity(safe,stage)
            if activity: emit(activity)
    readers=[threading.Thread(target=read,args=(process.stdout,'out'),daemon=True),threading.Thread(target=read,args=(process.stderr,'err'),daemon=True)]
    for reader in readers: reader.start()
    timed_out=False
    try:
        with contextlib.suppress(BrokenPipeError):
            process.stdin.write(prompt); process.stdin.close()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True; p.kill_tree(process); process.wait(timeout=20)
    finally:
        for reader in readers: reader.join(timeout=5)
        process.stdout.close(); process.stderr.close()
        if handle: handle.close()
        with p.CHILD_LOCK: p.CHILDREN.discard(process)
    if timed_out:
        raise RuntimeError('Local timeout; completed files retained, interrupted costs require reconciliation.')
    return process.returncode,''.join(captured['out']),''.join(captured['err'])


def _run_request(request):
    global _JOB
    job_id=request['job_id']
    _JOB=p.job_path(job_id)
    config=p.config()
    task=p.load(_JOB/'task.json')
    import studio_tasks
    config['_task_kind']=studio_tasks.install(p,config,_JOB)
    configured_job_cap=p.cost_decimal(config.get('worker_budget_usd_per_job',3),'Configured job cap')
    config['worker_budget_usd_per_job']=float(min(configured_job_cap,p.cost_decimal(task.get('budget_usd',configured_job_cap),'Task job cap')))
    for name,value in request.get('secrets',{}).items():
        if isinstance(value,str) and value:
            p.SECRETS['provider:'+name]=value
    if request.get('secrets',{}).get('claude-code'):
        p.SECRETS['claude']=request['secrets']['claude-code']
    if request.get('secrets',{}).get('openrouter-research'):
        p.SECRETS['research']=request['secrets']['openrouter-research']
    private_profile=p.profiles_for(config)/'studio-jobs'/job_id
    original_auth=p.profiles_for(config)/'codex'/'auth.json'
    target=private_profile/'codex'/'auth.json'
    adapters.private_profile_path(p,config,original_auth)
    adapters.private_profile_path(p,config,target)
    target.parent.mkdir(parents=True,exist_ok=True)
    if original_auth.is_file(): shutil.copyfile(original_auth,target)
    config['profiles_dir']=str(private_profile)
    config['godot_allowed_project']=str(_JOB/'game')
    config['godot_preapprove_launch']=True
    approval=request.get('paid_approval',{})
    allowed=set(request.get('allowed_metered_providers') or approval.get('providers',[])) if approval.get('approved') else set()
    p.APPROVE_PAID=bool(allowed)
    if approval.get('job_budget_usd') is not None:
        approved=p.cost_decimal(approval['job_budget_usd'],'Approved API budget')
        configured=p.cost_decimal(config.get('worker_budget_usd_per_job',3),'Configured API budget')
        if approved<=0 or approved>configured:
            raise RuntimeError('The approved budget must be positive and within the saved task cap.')
        config['worker_budget_usd_per_job']=float(approved)
    config['pause_grace_seconds']=min(60,max(0,float(config.get('pause_grace_seconds',30))))
    config['_provider_secrets']=dict(request.get('secrets',{}))
    config['_mcp_environment']={name.split(':',1)[1]:value for name,value in request.get('secrets',{}).items() if name.startswith('mcp-variable:')}
    p.process=live_process
    base_prompt=p.base_prompt
    def prompt_with_policy(job,role):
        prompt=base_prompt(job,role)
        if config.get('studio_policy',{}).get('token_saving',True):
            return prompt+'\nToken-saving policy: concise visible summaries, bounded tool work, no repeated polling or redundant completed work.\n'
        return prompt.replace('Keep summary under 120 words.','Provide a detailed visible delivery explanation and verification notes, up to 600 words. Waiting still ends this invocation.')
    p.base_prompt=prompt_with_policy
    # Each actionable role validates its own engine/MCP later.
    prepare_shared_schemas(config)
    prepared=set()
    def preflight(module,c,job,stages):
        adapters.freeze_registry(module,c,job)
        for stage in stages:
            if stage not in adapters.STAGE_ROLES: continue
            provider=adapters.provider_for(module,c,job,stage)
            if provider.billing=='metered' and provider.provider_id not in allowed:
                raise RuntimeError('等待费用授权：'+provider.provider_id+' 未获本次 API 预算批准；其他独立工位可继续。')
            if provider.adapter=='codex':
                module.check_auth(adapters.native_config(module,c,provider))
            elif provider.adapter in ('claude','kimi'):
                adapters.native_config(module,c,provider,allow_default_prompt=True)
            elif provider.adapter=='openrouter-research':
                secret=c['_provider_secrets'].get(provider.provider_id)
                if secret: module.SECRETS['research']=secret
                else: module.need_secret('research')
            if c.get('_task_kind')!='software' and adapters.STAGE_ROLES[stage]=='vfx' and stage not in prepared and provider.adapter in ('codex','claude','kimi'):
                port=studio_blender.ensure_instance(module.ROOT,job.name,c,emit)
                c['blender_port']=port
                c['blender_mcp_args']=['--host','127.0.0.1','--port',str(port)]
                prepared.add(stage)
    adapters.preflight=preflight
    def checkpoint(c,job):
        if c.get('_task_kind')=='software':
            return {'status':'not_required','saved':True,'message':'软件资源已由文件工作区和版本检查点保存；此任务不使用 Blender。'}
        try:
            result=studio_blender.save_checkpoint(p.ROOT,job.name,c.get('blender_port',9877))
            emit({'type':'activity','stage':'claude_vfx','activity':'暂停检查点：独立 Blender 场景已保存','level':'info'})
            return {'status':'saved',**result}
        except Exception as error:
            return {'status':'unverified','saved':False,'message':p.redacted(str(error))}
    config['_checkpoint_hook']=checkpoint
    autosave_stop=threading.Event()
    def autosave():
        while not autosave_stop.wait(15):
            if config.get('studio_policy',{}).get('autosave',True):
                try:
                    p.checkpoint_job(config,_JOB,'自动保存当前任务进度',state='RUNNING',save_art=False)
                    emit({'type':'checkpoint','activity':'任务状态与已写文件进度已自动保存','level':'info'})
                except Exception as error:
                    emit({'type':'checkpoint','activity':'自动保存待重试','text':p.redacted(str(error)),'level':'error'})
    autosave_thread=threading.Thread(target=autosave,daemon=True); autosave_thread.start()
    emit({'type':'runner','status':'running','activity':'后台工位开始处理；仅启动已就绪、已授权的任务。','level':'info'})
    runtime=_JOB/'.studio-runner.json'
    p.dump(runtime,{'pid':os.getpid(),'job_id':job_id,'status':'running','started_at':time.time(),'profiles_dir':str(private_profile)})
    try:
        action=request.get('action','start')
        if action=='resume': p.resume_job(config,job_id)
        elif action=='retry':
            stage=request.get('stage')
            rows=p.stage_rows(job_id)
            if stage not in rows or rows[stage]['status'] not in ('FAILED','BLOCKED','INTERRUPTED'):
                raise RuntimeError('Only a failed, blocked or interrupted stage can be explicitly retried.')
            if p.checkpoint_cost_state(config,_JOB,'PAUSED')['cost_reconciliation_required']:
                raise RuntimeError('Actual interrupted API costs must be reconciled before a retry.')
            p.set_stage(job_id,stage,'READY','Explicit desktop retry; invocation counter and budget preserved.')
            if stage=='gpt_integrate': p.set_stage(job_id,'qa','DORMANT','Retest repaired integration.')
            p.run_job(config,job_id)
        else: p.run_job(config,job_id)
        rows=p.stage_rows(job_id)
        status='completed' if rows['qa']['status']=='DONE' else 'paused' if (_JOB/'pause.request').exists() else 'waiting'
        autosave_stop.set(); autosave_thread.join(timeout=5)
        p.checkpoint_job(config,_JOB,'Controller saved final runtime state',state=status.upper(),save_art=False)
        saved=studio_snapshots.save(p,_JOB,status)
        emit({'type':'checkpoint','activity':'文件版本检查点已保存','path':saved['path'],'level':'info'})
        emit({'type':'runner','status':status,'activity':'任务完成，可打开交付。' if status=='completed' else '进度已保存，等待所需连接、资料或恢复操作。','level':'info'})
        p.dump(runtime,{'pid':os.getpid(),'job_id':job_id,'status':status,'ended_at':time.time()})
        return 0
    except Exception as error:
        autosave_stop.set(); autosave_thread.join(timeout=5)
        emit({'type':'runner','status':'needs_attention','activity':p.redacted(str(error)),'level':'error'})
        p.dump(runtime,{'pid':os.getpid(),'job_id':job_id,'status':'needs_attention','ended_at':time.time(),'detail':p.redacted(str(error))})
        return 1


def main():
    from studio_controller_lifecycle import controller_owned
    request=json.loads(sys.stdin.read());job=p.job_path(request['job_id'])
    def owner_lost(reason):
        p.request_pause(job.name,reason)
        emit({'type':'runner','status':'pausing','job_id':job.name,'activity':'后台已退出，正在暂停并保存当前任务。'})
    from studio_task_cycles import pipeline_edit_lease,ensure_task_startable
    from studio_pipeline_delete import reject_deleted_id
    with pipeline_edit_lease(job):
        reject_deleted_id(p.ROOT,job.name)
        ensure_task_startable(job)
        with controller_owned(request,owner_lost) as guard:
            original=p.run_job
            def owned_run(*args,**kwargs):
                if guard is not None:guard.refresh()
                return original(*args,**kwargs)
            p.run_job=owned_run
            try:
                if guard is not None and guard.lost.is_set():request={**request,'action':'start'}
                return _run_request(request)
            finally:p.run_job=original


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        emit({'type':'runner','status':'needs_attention','activity':p.redacted(str(error)),'level':'error'})
        raise SystemExit(1)
