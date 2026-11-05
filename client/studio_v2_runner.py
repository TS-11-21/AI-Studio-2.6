"""One independent V2 pipeline per owned private subprocess; no model polling."""
from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from studio_runner import public_activity

_RUN_ENGINE=None


def member_process(p,args,cwd,env,*,emit,prompt='',timeout=120,log=None):
    from studio_controller_lifecycle import check_dispatch
    check_dispatch()
    if p.STOP.is_set(): raise RuntimeError('Paused; no new process is launched.')
    p.ensure_plain_path(Path(cwd))
    from studio_windows_paths import native_cwd
    launch_cwd = native_cwd(cwd)
    if log: p.ensure_plain_path(log)
    options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
    process=subprocess.Popen(args,cwd=launch_cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        encoding='utf-8',errors='replace',**options)
    with p.CHILD_LOCK: p.CHILDREN.add(process)
    captured={'out':[],'err':[]}; lock=threading.RLock(); handle=None
    if log:
        log.parent.mkdir(parents=True,exist_ok=True); handle=log.open('w',encoding='utf-8')
    def collect(stream,kind):
        for line in iter(stream.readline,''):
            safe=p.redacted(line)
            with lock:
                captured[kind].append(safe)
                if handle: handle.write(safe); handle.flush()
            event=public_activity(safe,'member')
            if event: emit(event)
    readers=[threading.Thread(target=collect,args=(process.stdout,'out'),daemon=True),threading.Thread(target=collect,args=(process.stderr,'err'),daemon=True)]
    for reader in readers: reader.start()
    expired=False
    try:
        with contextlib.suppress(BrokenPipeError): process.stdin.write(prompt); process.stdin.close()
        try: process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            expired=True; p.kill_tree(process); process.wait(timeout=20)
    finally:
        for reader in readers: reader.join(timeout=5)
        process.stdout.close(); process.stderr.close()
        if handle: handle.close()
        with p.CHILD_LOCK: p.CHILDREN.discard(process)
    if expired: raise RuntimeError('The owned member process timed out; actual paid costs may require reconciliation.')
    return process.returncode,''.join(captured['out']),''.join(captured['err'])


def _run_request(request):
    global _RUN_ENGINE
    _RUN_ENGINE=None
    from studio_v2_engine import V2Engine, plain, read
    from studio_v25_changes import recover_plan_update
    root=Path(__file__).resolve().parent
    identifier=request.get('pipeline_id') or request['job_id']; job=plain(root/'jobs'/identifier)
    recover_plan_update(job)
    # The saved plan is authoritative; a stale UI request cannot replace it.
    plan=read(job/'pipeline.v2.json')
    if request.get('plan') is not None and request['plan']!=plan:
        raise RuntimeError('The requested plan differs from the saved frozen pipeline.')
    lock=threading.RLock(); stdout_available=True
    def event(value):
        nonlocal stdout_available
        with lock:
            if not stdout_available: return
            try: print(json.dumps(value,ensure_ascii=False),flush=True)
            except (BrokenPipeError,OSError): stdout_available=False
    approval=dict(request.get('paid_approval',{}))
    if 'allowed_metered_members' in request: approval['members']=request['allowed_metered_members']
    engine=V2Engine(root,job,plan,approval=approval,secrets=request.get('secrets',{}),event_callback=event)
    _RUN_ENGINE=engine
    from studio_controller_lifecycle import _ACTIVE_GUARD, guarded_recovery
    engine.recover=guarded_recovery(_ACTIVE_GUARD,engine.recover)
    def pause(signum,frame):
        with contextlib.suppress(RuntimeError,ValueError,OSError):
            with engine.write_lease():
                engine.dump(job/'pause.request',{'reason':'Owned runner received a stop signal.','signal':signum,'at':time.time()})
    for name in ('SIGINT','SIGTERM','SIGBREAK'):
        if hasattr(signal,name): signal.signal(getattr(signal,name),pause)
    runtime=job/'.studio-runner.json'
    try:
        with engine.write_lease():
            engine.dump(runtime,{'pid':os.getpid(),'job_id':identifier,'pipeline_id':identifier,'status':'running','started_at':time.time(),'generation':2})
            engine.emit({'type':'runner','status':'running','activity':'独立二代流水线已开始，成员并行且分别记账。'})
        result=engine.run(request.get('action','start'),request.get('member_id'))
        status='completed' if result['status']=='DONE' else 'paused' if result['status']=='PAUSED' else 'waiting'
        with engine.write_lease():
            engine.dump(runtime,{'pid':os.getpid(),'job_id':identifier,'pipeline_id':identifier,'status':status,'ended_at':time.time(),'generation':2})
            engine.emit({'type':'runner','status':status,'activity':'真实验收与交付均完成。' if status=='completed' else '团队进度已保存；等待所需连接、资料或明确恢复。'})
        return 0
    except Exception as error:
        try:
            with engine.write_lease():
                engine.state.update(status='NEEDS_ATTENTION',error=engine.safe(str(error)))
                engine.persist()
                engine.dump(runtime,{'pid':os.getpid(),'job_id':identifier,'pipeline_id':identifier,'status':'needs_attention','ended_at':time.time(),'detail':engine.safe(str(error)),'generation':2})
                engine.emit({'type':'runner','status':'needs_attention','activity':engine.safe(str(error)),'level':'error'})
        except (RuntimeError,ValueError,OSError):
            # Deleted/unavailable jobs retain their outer audit; never recreate them.
            event({'type':'runner','status':'needs_attention','job_id':identifier,'activity':engine.safe(str(error)),'level':'error'})
        return 1


def main():
    global _RUN_ENGINE
    _RUN_ENGINE=None
    from studio_controller_lifecycle import controller_owned
    from studio_v2_engine import plain, pipeline_lease
    import pipeline as pipeline_module
    from studio_runner import emit as public_emit
    request=json.loads(sys.stdin.read())
    identifier=request.get('pipeline_id') or request['job_id']
    job=plain(Path(__file__).resolve().parent/'jobs'/identifier)
    def owner_lost(reason):
        with contextlib.suppress(RuntimeError,ValueError,OSError):
            write_scope=(_RUN_ENGINE.write_lease() if _RUN_ENGINE is not None else pipeline_lease(job))
            with write_scope:
                pipeline_module.ensure_plain_path(job)
                pipeline_module.dump(job/'pause.request',{'reason':reason,'at':time.time(),'controller_exit':True})
                public_emit({'type':'runner','status':'pausing','job_id':identifier,'activity':'后台已退出，正在暂停并保存当前任务。'})
    with controller_owned(request,owner_lost):return _run_request(request)


if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as error:
        # No private request/configuration is ever dumped on this error path.
        print(json.dumps({'type':'runner','status':'needs_attention','activity':type(error).__name__+': check the local saved pipeline state.'}),flush=True)
        raise SystemExit(1)
