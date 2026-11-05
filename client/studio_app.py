"""Loopback-only desktop web application with explicit job and API controls."""
from __future__ import annotations
import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import webbrowser

from studio_private import SecretStore
from studio_service import StudioService
from studio_transport import DesktopTransport
from studio_wakeup import LocalWakeup

ROOT=Path(__file__).resolve().parent


class Application:
    def __init__(self,root=ROOT,transport=None,secret_store=None):
        self.root=Path(root).resolve()
        self.generation=2 if (self.root/'config.local.json').is_file() and json.loads((self.root/'config.local.json').read_text(encoding='utf-8-sig')).get('studio_generation')==2 else 1
        configuration=json.loads((self.root/'config.local.json').read_text(encoding='utf-8-sig')) if (self.root/'config.local.json').is_file() else {}
        self.version=str(configuration.get('studio_version','2.0' if self.generation==2 else '1.0'))
        if self.version in ('2.6 上架准备','2.6 发布候选'):
            self.version='2.6'
        self.root_id=hashlib.sha256(str(self.root).encode()).hexdigest()[:24]
        self.token=secrets.token_urlsafe(32)
        private=Path(configuration.get('runtime_private_dir',str(self.root.parents[1]/'work'/('ai-studio-v25-runtime' if self.version=='2.5' else 'ai-studio-v2-runtime' if self.generation==2 else 'ai-studio-runtime'))))
        self.secrets=secret_store or SecretStore(private/'studio-credentials.dpapi')
        self.transport=transport or DesktopTransport(self.root,self.secrets)
        if self.generation==2:
            from studio_v2_service import StudioV2Service
            self.service=StudioV2Service(self.root,transport=self.transport,redactor=self.secrets.redact)
        else:
            self.service=StudioService(self.root,transport=self.transport,redactor=self.secrets.redact)
        self.checks=set()
        self.guard=threading.RLock()
        if self.generation==2:
            from studio_v2_wakeup import PipelineWakeups
            self.wakeup=PipelineWakeups(self.service,self.root,event_callback=lambda event:self.service._events_for(event['job_id'],event),redactor=self.secrets.redact)
        else:
            self.wakeup=LocalWakeup(self.service,self.root,event_callback=lambda event:self.service._events_for(event['job_id'],event),redactor=self.secrets.redact)
        self.transport.remember_request=self.wakeup.remember
        original_state=self.service.get_state
        def task_state(job_id=None):
            from store_cancel import decorate_state
            result=decorate_state(self,original_state(job_id))
            from studio_pipeline_delete import decorate_deleted_state
            result=decorate_deleted_state(self,result)
            result.setdefault('app',{}).setdefault('capabilities',{}).update(task_run_cancellation=True,cancellation_semantics='keep_pipeline_shell',backend_pause_exit=True,pipeline_deletion=True)
            return result
        self.service.get_state=task_state


    def pause_on_exit(self):
        self.wakeup.stop()
        jobs=self.service.get_state().get('jobs',[])
        active=[job['job_id'] for job in jobs if job.get('running') or job.get('runtime',{}).get('running') or str(job.get('status','')).upper().startswith('WAITING')]
        for job_id in active:
            self.wakeup.forget(job_id,'后台退出，默认暂停并保存。')
            self.service.pause_job(job_id,'Controller exit requested a saved pause.')
        return {'pause_requested_jobs':active}

    def dispatch(self,path,data):
        service=self.service
        if path=='/api/pipelines/delete':
            from studio_pipeline_delete import delete_pipeline
            return delete_pipeline(self,data.get('job_id'),data.get('confirmation'))
        if path=='/api/jobs/cancel':
            from store_cancel import cancel_job
            self.wakeup.forget(data.get('job_id'),'用户取消本次任务；流水线保留。')
            return cancel_job(self,data.get('job_id'),data.get('confirmation'))
        if path in ('/api/job/start','/api/job/resume','/api/job/retry'):
            from store_cancel import reject_cancelled
            from studio_pipeline_delete import reject_deleted_id
            reject_deleted_id(self.root,data.get('job_id'))
            reject_cancelled(self,data.get('job_id'))
        if path=='/api/job/update':
            if self.generation!=1:raise ValueError('图式流水线请使用对应版本的编辑入口。')
            self.wakeup.forget(data['job_id'],'保存新的任务内容后，执行前重新确认工位与费用。')
            return service.update_task(data['job_id'],goal=data.get('goal'),budget_usd=data.get('budget_usd'),task_patch=data.get('task_patch'))

        if path.startswith('/api/v25/'):
            if self.version not in ('2.5','2.6'): raise ValueError('需要使用 2.6 版入口。')
            if path=='/api/v25/budget/requests': return service.budget_requests(data['job_id'])
            if path=='/api/v25/budget/decide':
                self.wakeup.forget(data['job_id'],'预算请求由操作员处理；再次执行需要重新确认。')
                return service.decide_budget_request(data['job_id'],data['request_id'],data['approved'],
                    increase_usd=data.get('increase_usd',0),expected_revision=data.get('expected_revision'))
            raise ValueError('未知 2.5 版操作。')
        if path.startswith('/api/v2/'):
            if self.generation!=2: raise ValueError('需要使用 2 代入口。')
            if path=='/api/v2/pipelines/create': return service.create_pipeline(data)
            if path=='/api/v2/pipelines/update':
                self.wakeup.forget(data['pipeline_id'],'用户修改本线任务或预算；执行前需要重新确认。')
                return service.update_pipeline(data['pipeline_id'],data)
            if path=='/api/v2/pipelines/clone': return service.clone_pipeline(data['source_id'],data['pipeline_id'])
            if path=='/api/v2/pipelines/plan': return service.get_plan(data['pipeline_id'])
            raise ValueError('未知 2 代操作。')
        if path=='/api/app/exit':
            result=self.pause_on_exit()
            server=getattr(self,'http_server',None)
            if server is not None: threading.Thread(target=server.shutdown,daemon=True).start()
            return {**result,'shutting_down':True}
        if path=='/api/settings': return service.save_settings(data)
        if path=='/api/job/create':
            return service.create_job(data['job_id'],goal=data.get('goal'),budget_usd=data.get('budget_usd'),task_patch=data.get('task_patch'))
        if path in ('/api/job/start','/api/job/resume','/api/job/retry'):
            self.wakeup.forget(data['job_id'],'重新确认本次任务工位与费用')
            options={'paid_approval':data.get('paid_approval',{})}
            if path.endswith('/start'): return service.start_job(data['job_id'],**options)
            if path.endswith('/resume'): return service.resume_job(data['job_id'],**options)
            if self.generation==2:
                return service.retry_job(data['job_id'],data.get('stage'),member_id=data.get('member_id'),**options)
            return service.retry_job(data['job_id'],data['stage'],**options)
        if path=='/api/job/pause':
            self.wakeup.forget(data['job_id'],'操作员明确暂停')
            return service.pause_job(data['job_id'],'桌面控制面板请求暂停并自动保存')
        if path=='/api/job/reconcile':
            if data.get('confirmed_from_usage') is not True:
                raise ValueError('请先从供应商实际用量记录确认费用。')
            if self.transport.get_job_runtime(data['job_id']).get('running'):
                raise ValueError('请等待正在运行的调用结束后核对费用。')
            import pipeline as p
            if self.generation==2:
                from studio_v2_budget import TeamBudgetLedger
                job=service._job(data['job_id']); plan=service.get_plan(data['job_id']); path=job/'.team-budget.json'
                if not path.is_file(): raise ValueError('本流水线没有待核对账本。')
                saved=p.load(path); invocation=data['invocation_id']; record=saved.get('invocations',{}).get(invocation)
                if not record or record.get('state') not in ('pending','unknown'): raise ValueError('只能核对未知或中断的成员调用。')
                cost=p.cost_decimal(data['reported_cost_usd'],'供应商实际费用')
                usage=None; metadata=None
                usage_path=record.get('native_usage_path') or record.get('metadata',{}).get('native_usage_path')
                if usage_path:
                    original=Path(usage_path)
                    target=original if original.is_absolute() else job/original
                    p.ensure_plain_path(target)
                    stages={'gameplay':'gpt_base','vfx':'claude_vfx','integration':'gpt_integrate','research':'research'}
                    stage=stages.get(record.get('role'))
                    if not stage: raise ValueError('费用记录中的工位无效。')
                    member_root=job/'.team'/stage/record['member']
                    p.ensure_plain_path(member_root)
                    if not target.absolute().is_relative_to(member_root.absolute()): raise ValueError('费用记录不属于这个成员。')
                    identity=record.get('metadata',{}).get('original_invocation','')
                    if not identity.startswith('native:'): raise ValueError('成员原生调用身份缺失。')
                    suffix=Path(identity[len('native:'):])
                    if suffix.is_absolute() or '..' in suffix.parts or tuple(target.parts[-len(suffix.parts):])!=tuple(suffix.parts): raise ValueError('费用路径与原始调用不匹配。')
                    usage=p.within(job,target.absolute().relative_to(job.absolute()).as_posix())
                    if '.pipeline' not in usage.parts or not usage.name.endswith('.usage.json'): raise ValueError('成员费用记录位置无效。')
                    metadata=p.load(usage) if usage.is_file() else {'is_smoke':False}
                    metadata.update(reported_total_cost_usd=float(cost),cost_known=True,cost_source='operator_verified_provider_usage')
                roles={role:spec['budget_usd'] for role,spec in plan['roles'].items()}
                members={member['id']:{'role':role,'budget_usd':member['budget_usd']} for role,spec in plan['roles'].items() for member in spec['members']}
                ledger=TeamBudgetLedger(path,str(plan['total_budget_usd']),roles,members)
                ledger.reconcile(invocation,cost)
                if usage is not None: p.dump(usage,metadata)
                return {'reconciled':True,'budget':ledger.snapshot(),'progress_preserved':True}
            from adapter_budget import BudgetLedger
            job=service._job(data['job_id']); path=job/'.adapter-state'/'budget.json'
            if not path.is_file(): raise ValueError('该任务没有待核对的 API 账本。')
            ledger_data=p.load(path); invocation=data['invocation_id']
            record=ledger_data.get('invocations',{}).get(invocation)
            if not record or record.get('state') not in ('unknown','pending'):
                raise ValueError('只能核对费用未知或被中断的调用。')
            cost=p.cost_decimal(data['reported_cost_usd'],'供应商实际费用')
            ledger=BudgetLedger(path,p.cost_decimal(ledger_data['total_cap_usd'],'已保存预算'))
            usage=None
            metadata=None
            if invocation.startswith('native:'):
                usage=p.within(job,invocation[len('native:'):])
                if '.pipeline' not in usage.parts or not usage.name.endswith('.usage.json'):
                    raise ValueError('未知调用的文件位置不属于该任务。')
                metadata=p.load(usage) if usage.is_file() else {'is_smoke':False}
                metadata.update(reported_total_cost_usd=float(cost),cost_known=True,cost_source='operator_verified_provider_usage')
            ledger.reconcile(invocation,cost)
            if usage is not None:
                p.dump(usage,metadata)
            return {'reconciled':True,'budget':ledger.snapshot(),'progress_preserved':True}
        if path=='/api/job/open': return self.transport.open_target(data['job_id'],data.get('target','delivery'))
        if path=='/api/providers':
            action=data['action']; provider_id=data['id']
            if action=='remove': return service.remove_provider(provider_id)
            provider=data.get('provider',{})
            if data.get('settings'): provider={**provider,'settings':data['settings']}
            if action=='add': return service.add_provider(provider_id,provider)
            if action=='update': return service.update_provider(provider_id,provider)
            if action=='bind': return service.bind_role(data['role'],provider_id)
            raise ValueError('Unknown provider action.')
        if path=='/api/mcp':
            action=data['action']
            if action=='import': return service.import_mcp_json(data['config'])
            if action=='remove': return service.remove_mcp_entry(data['id'])
            if action in ('add','update','toggle'): return service.save_mcp_entry(data['id'],data['entry'])
            raise ValueError('Unknown MCP action.')
        if path=='/api/credentials':
            self.secrets.set(data['provider_id'],data['secret'],bool(data.get('remember')))
            return {'configured':True,'remembered':bool(data.get('remember'))}
        if path=='/api/connections/check':
            provider_id=data.get('provider_id')
            if data.get('test_model'):
                if provider_id not in service.get_settings()['providers']:
                    raise ValueError('该 AI 未登记，不能启动连接实测。')
                approval=data.get('paid_approval',{})
                if approval.get('approved') is not True or provider_id not in approval.get('providers',[]):
                    return {'requires_paid_confirmation':True,'connection_budget_usd':.2,'providers':[provider_id]}
                cap=float(approval.get('connection_budget_usd',.2))
                if not 0<cap<=.2: raise ValueError('连接测试预算必须为最多 0.20 美元。')
                with self.guard:
                    if provider_id in self.checks: return {'status':'CHECKING','already_running':True}
                    self.checks.add(provider_id)
                def check():
                    try:
                        self.transport.test_connections([provider_id],limit_usd=cap)
                    except Exception as error:
                        with self.transport.lock:
                            self.transport.connections[provider_id]={'status':'ERROR','detail':self.secrets.redact(str(error)),'checked_at':time.time()}
                    finally:
                        with self.guard: self.checks.discard(provider_id)
                self.transport.connections[provider_id]={'status':'CHECKING','detail':'正在进行明确批准的连接实测','checked_at':time.time()}
                threading.Thread(target=check,daemon=True).start()
                return {'status':'CHECKING','submitted':True,'threshold_usd':cap}
            result=service.check_connections(provider_id=provider_id)
            rows=result.get('connections',{})
            result['requires_paid_confirmation']=any(row.get('status')=='CONFIGURED' for row in rows.values())
            return result
        if path=='/api/login':
            selected=service.get_settings()['providers'].get(data.get('provider_id'))
            if not selected or selected.get('adapter')!='codex': raise ValueError('仅已登记的 Codex 订阅接入使用浏览器登录。')
            import pipeline as p
            c=p.config()
            subprocess.Popen(p.command_for('codex',c)+['login'],env=p.env_for('codex',c),cwd=str(self.root),
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            return {'login_started':True,'detail':'请在官方登录窗口亲自完成登录或 2FA。'}
        raise ValueError('Unknown action.')


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass

    def respond(self,status,data,content_type='application/json; charset=utf-8'):
        body=json.dumps(data,ensure_ascii=False,default=str).encode('utf-8') if isinstance(data,(dict,list)) else data
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        app=self.server.app; path=urlsplit(self.path).path
        try:
            if path=='/api/health': return self.respond(200,{'ok':True,'app':'AI Studio '+app.version+'版' if app.generation==2 else 'AI Studio','root_id':app.root_id,'version':app.version})
            if path=='/api/state':
                state=app.service.get_state()
                state['app'].update(version=app.version+' 本机版',generation=app.generation,host='Windows',desktop='Codex',capabilities={**app.transport.capabilities,**state['app'].get('capabilities',{})})
                return self.respond(200,{'ok':True,'data':state})
            if path=='/api/settings': return self.respond(200,{'ok':True,'data':app.service.get_settings()})
            if path=='/api/assistant/templates':
                from assistant_templates import catalog
                return self.respond(200,{'ok':True,'data':{'templates':catalog()}})
            if path in ('/','/index.html'):
                content=(app.root/'ui'/'index.html').read_text(encoding='utf-8')
                content=content.replace('</head>','<script>window.STUDIO_TOKEN='+json.dumps(app.token)+';</script></head>')
                return self.respond(200,content.encode('utf-8'),'text/html; charset=utf-8')
            if path.startswith('/ui/'):
                file=(app.root/path.lstrip('/')).resolve()
                if not file.is_relative_to(app.root/'ui') or not file.is_file(): return self.respond(404,{'error':'Not found'})
                return self.respond(200,file.read_bytes(),(mimetypes.guess_type(file.name)[0] or 'application/octet-stream')+'; charset=utf-8')
            return self.respond(404,{'ok':False,'error':'Not found'})
        except Exception as error:
            return self.respond(500,{'ok':False,'error':app.secrets.redact(str(error))})

    def do_POST(self):
        app=self.server.app
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=2*1024*1024: raise ValueError('Request size is invalid.')
            self.connection.settimeout(5)
            body=self.rfile.read(length)
        except Exception as error:
            return self.respond(400,{'ok':False,'error':app.secrets.redact(str(error))})
        origin=self.headers.get('Origin')
        allowed='http://127.0.0.1:'+str(self.server.server_port)
        if origin and origin!=allowed:
            return self.respond(403,{'ok':False,'error':'仅允许本地控制面板操作。'})
        if not hmac.compare_digest(self.headers.get('X-Studio-Token',''),app.token):
            return self.respond(403,{'ok':False,'error':'控制面板会话已失效，请重新打开桌面入口。'})
        try:
            data=json.loads(body)
            if not isinstance(data,dict): raise ValueError('Request body must be an object.')
            result=app.dispatch(urlsplit(self.path).path,data)
            return self.respond(200,{'ok':True,'data':result})
        except Exception as error:
            return self.respond(400,{'ok':False,'error':app.secrets.redact(str(error))})


def make_server(application,port=0):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True; server.app=application; application.http_server=server
    return server


def open_window(url):
    edge=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if edge.is_file():
        subprocess.Popen([str(edge),'--app='+url,'--window-size=1380,920','--no-first-run'],
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    else: webbrowser.open(url)


def main():
    parser=argparse.ArgumentParser()
    configuration=json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig'))
    parser.add_argument('--open',action='store_true'); parser.add_argument('--port',type=int,default=int(configuration.get('studio_port',43147 if configuration.get('studio_generation')==2 else 43127)))
    args=parser.parse_args()
    root_id=hashlib.sha256(str(ROOT).encode()).hexdigest()[:24]
    state_file=ROOT/'.control'/'studio-app.json'
    if state_file.is_file():
        try:
            saved=json.loads(state_file.read_text(encoding='utf-8'))
            url='http://127.0.0.1:'+str(saved['port'])
            health=json.loads(urllib.request.urlopen(url+'/api/health',timeout=2).read())
            if health.get('root_id')==root_id:
                if args.open: open_window(url)
                return
        except Exception: pass
    application=Application()
    server=None
    for port in range(args.port,args.port+20):
        try: server=make_server(application,port); break
        except OSError: continue
    if server is None: raise RuntimeError('没有可用的本地控制面板端口。')
    state_file.parent.mkdir(parents=True,exist_ok=True)
    state_file.write_text(json.dumps({'port':server.server_port,'pid':os.getpid(),'root_id':root_id}),encoding='utf-8')
    application.wakeup.start()
    if args.open: open_window('http://127.0.0.1:'+str(server.server_port))
    try: server.serve_forever(poll_interval=.5)
    finally:
        application.pause_on_exit()
        server.server_close()


if __name__=='__main__':
    if sys.stdout is None:
        configuration=json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig'))
        private=Path(configuration.get('runtime_private_dir',str(ROOT.parents[1]/'work'/'ai-studio-v25-runtime'))); private.mkdir(parents=True,exist_ok=True)
        sys.stdout=(private/'studio-server.log').open('a',encoding='utf-8'); sys.stderr=sys.stdout
    main()
