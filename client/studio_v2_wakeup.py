"""Session-scoped member handoffs, with no creation quota and no model polling."""
from copy import deepcopy
import hashlib,json,threading,time
from pathlib import Path
from worker_adapters import ProviderRegistry,TASK_VERSION,decode_completion

STAGES={'gameplay':'gpt_base','vfx':'claude_vfx','integration':'gpt_integrate','research':'research'}

def signature(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

class PipelineWakeups:
    def __init__(self,service,root,event_callback=None,redactor=None,poll_seconds=1):
        self.service=service; self.root=Path(root).resolve(); self.event_callback=event_callback
        self.redactor=redactor or (lambda value:str(value)); self.poll_seconds=poll_seconds
        self.entries={}; self.lock=threading.RLock(); self.tick_lock=threading.Lock(); self.stop_event=threading.Event(); self.thread=None

    def remember(self,job_id,request):
        plan=deepcopy(request.get('plan') or self.service.get_plan(job_id))
        approval=deepcopy(request.get('paid_approval',{}))
        safe={'plan':plan,'paid_approval':approval}
        with self.lock:
            previous=self.entries.get(job_id)
            if previous is not None and previous['signature']==signature(safe): return False
            if previous is not None:
                previous['disabled']=True
                return False
            self.entries[job_id]={'request':safe,'signature':signature(safe),'plan_digest':signature(plan),'seen':{},'wakes':0,'disabled':False}
        return True

    def forget(self,job_id,reason='Explicit operator action'):
        with self.lock: return self.entries.pop(job_id,None) is not None

    def emit(self,job_id,message,level='info',member_id=None):
        event={'type':'wakeup','job_id':job_id,'text':message,'activity':message,'level':level,'at':time.time()}
        if member_id: event['member_id']=member_id
        if self.event_callback: self.event_callback(event)

    def completion(self,job,member,member_plan,plan):
        stage=STAGES[member_plan['role']]
        if member.get('stage',stage)!=stage: raise ValueError('Handoff stage does not belong to its selected member.')
        workspace=Path(member['workspace']).absolute()
        owned=(job/'.team'/stage/member_plan['id']).absolute()
        if workspace.resolve()!=workspace or owned.resolve()!=owned or not workspace.is_relative_to(owned): raise ValueError('Member workspace is outside its own candidate directory.')
        relative=workspace.relative_to(owned).parts
        if relative and (member_plan['role']!='research' or len(relative)!=2 or relative[0]!='requests'):
            raise ValueError('Handoff workspace does not match the selected member or research request.')
        packet=workspace/'.adapter-state'/'tasks'/stage/'task.json'
        finished=packet.with_name('completion.json')
        if packet.resolve()!=packet.absolute() or finished.resolve()!=finished.absolute(): raise ValueError('Redirected handoff files cannot wake a member.')
        if not finished.is_file(): return None
        stat=finished.stat()
        if stat.st_size>1024*1024 or packet.stat().st_size>1024*1024: raise ValueError('Handoff file is too large.')
        task=json.loads(packet.read_text(encoding='utf-8-sig'))
        result=json.loads(finished.read_text(encoding='utf-8-sig'))
        if finished.stat().st_mtime_ns!=stat.st_mtime_ns: return None
        if task.get('version')!=TASK_VERSION or task.get('provider_id')!=member_plan['provider_id'] or task.get('role')!=member_plan['role']:
            raise ValueError('Handoff identity does not match its selected member.')
        expected=workspace/('art' if member_plan['role']=='vfx' else 'references' if member_plan['role']=='research' else 'game')
        if Path(task.get('inputs',{}).get('workspace','')).resolve()!=expected.resolve(): raise ValueError('Handoff input workspace mismatch.')
        providers=plan['providers']
        routes={role:plan['roles'][role]['members'][0]['provider_id'] for role in STAGES}
        routes[member_plan['role']]=member_plan['provider_id']
        registry=ProviderRegistry.from_config({'providers':providers,'role_routes':routes},include_defaults=False)
        if registry.get_provider(member_plan['provider_id']).adapter!='manual-desktop': return None
        route=registry.plan_task(task['task_id'],task['role'],task['prompt'],provider_id=task['provider_id'],inputs=task['inputs'],exchange_id=task['exchange_id'])
        decode_completion(route,result)
        return stat.st_mtime_ns

    @staticmethod
    def blocked(row,job):
        team=row.get('team') or {}
        active={'RUNNING','CHECKING','CURATING'}
        if row.get('running') or row.get('runtime',{}).get('running') or any(member.get('status') in active for member in team.get('members',{}).values()):
            return True
        budget=row.get('budget') or {}
        if budget.get('unknown_invocations') or budget.get('interrupted_invocations') or budget.get('cost_reconciliation_required') or (budget.get('blocked') and budget.get('costs_complete') is False):
            return True
        if row.get('status') in ('PAUSED','PAUSING') or team.get('status') in ('PAUSED','PAUSING') or any((job/name).exists() for name in ('pause.request','.controlpause')):
            return True
        checkpoint=job/'checkpoint'/'metadata.json'
        if checkpoint.exists():
            if checkpoint.resolve()!=checkpoint.absolute(): return True
            if json.loads(checkpoint.read_text(encoding='utf-8-sig')).get('state') in ('PAUSED','PAUSING'): return True
        return False

    def tick(self):
        if not self.tick_lock.acquire(False): return
        try:
            with self.lock: pairs=list(self.entries.items())
            for job_id,entry in pairs:
                if entry['disabled']: continue
                try:
                    state=self.service.get_state(job_id)
                    row=next((item for item in state.get('jobs',[]) if item['job_id']==job_id),None)
                    if row is None: continue
                    job=self.root/'jobs'/job_id
                    if row.get('status') in ('DONE','COMPLETED'):
                        self.forget(job_id); continue
                    if row.get('status') in ('PAUSED','PAUSING') or any((job/name).exists() for name in ('pause.request','.controlpause')):
                        self.forget(job_id); continue
                    if self.blocked(row,job): continue
                    plan=self.service.get_plan(job_id)
                    if signature(plan)!=entry['plan_digest']:
                        entry['disabled']=True; self.emit(job_id,'本流水线配置已变化，请重新确认后恢复。'); continue
                    selected={member['id']:{**member,'role':role} for role,role_plan in plan['roles'].items() for member in role_plan['members']}
                    team=row.get('team',{}); ready=[]
                    for member_id,member in team.get('members',{}).items():
                        if member.get('status')!='WAITING_EXTERNAL' or member_id not in selected: continue
                        try: changed=self.completion(job,member,selected[member_id],plan)
                        except (OSError,ValueError,KeyError) as error:
                            key=(member_id,str(error))
                            if key not in entry['seen']:
                                entry['seen'][key]=True; self.emit(job_id,'交接文件待校验：'+self.redactor(str(error))[:400],'error',member_id)
                            continue
                        if changed is not None and entry['seen'].get(member_id)!=changed: ready.append((member_id,changed))
                    if not ready: continue
                    limit=int(plan.get('policy',{}).get('max_file_wakeups',max(8,len(selected)*4)))
                    if entry['wakes']>=limit:
                        entry['disabled']=True; self.emit(job_id,'本次自动交接唤醒次数已用尽，请检查后明确恢复。'); continue
                    with self.lock:
                        if self.entries.get(job_id) is not entry: continue
                        current=next((item for item in self.service.get_state(job_id).get('jobs',[]) if item['job_id']==job_id),None)
                        if current is None or self.blocked(current,job): continue
                        if signature(self.service.get_plan(job_id))!=entry['plan_digest']: entry['disabled']=True; continue
                        for member_id,changed in ready: entry['seen'][member_id]=changed
                        entry['wakes']+=1
                        self.service.start_job(job_id,paid_approval=entry['request']['paid_approval'])
                    self.emit(job_id,'成员交接文件已就绪，按本会话原预算继续本流水线。')
                except Exception as error:
                    entry['disabled']=True; self.emit(job_id,'自动交接等待人工处理：'+self.redactor(str(error))[:400],'error')
        finally: self.tick_lock.release()

    def start(self):
        if self.thread and self.thread.is_alive(): return False
        self.stop_event.clear()
        def loop():
            while not self.stop_event.is_set():
                self.tick(); self.stop_event.wait(self.poll_seconds)
        self.thread=threading.Thread(target=loop,daemon=True,name='v2-member-handoffs'); self.thread.start(); return True

    def stop(self,timeout=2):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(timeout)
