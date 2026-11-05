"""Independent pipeline teams: isolated real workers, explicit curation, bounded waits.

Each member owns a fresh pipeline module and workspace. No module-level task
contract is switched beneath another member. The caller supplies one approved
plan; this module never picks a fallback provider or creates another pipeline.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import zipfile

import adapter_runtime
import studio_blender
import studio_tasks
from studio_v2_budget import TeamBudgetLedger
from studio_v25_changes import record_budget_request, load_budget_requests, recover_plan_update

ROLE_STAGE={'gameplay':'gpt_base','vfx':'claude_vfx','integration':'gpt_integrate','research':'research'}
EXCLUDED={'.git','.godot','.pipeline','.control','.adapter-state','.profiles','.mcp','__pycache__','team-handoffs'}
WAITING={'WAITING_CONNECTION','WAITING_EXTERNAL','WAITING_REFERENCE','WAITING_BUDGET','WAITING_RESERVATION','FAILED','BLOCKED','INTERRUPTED','BUDGET_STOP'}


def plain(path):
    path=Path(path).absolute()
    if path.resolve()!=path:
        raise RuntimeError('Redirected team paths are not supported.')
    return path


def read(path):
    return json.loads(plain(path).read_text(encoding='utf-8-sig'))


def digest(path):
    value=hashlib.sha256()
    with plain(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''): value.update(block)
    return value.hexdigest()


def project_files(base):
    base=plain(base)
    result={}
    if not base.exists(): return result
    for path in sorted(base.rglob('*')):
        relative=path.relative_to(base)
        if any(part in EXCLUDED or part.startswith('.') for part in relative.parts): continue
        if path.is_symlink(): raise RuntimeError('A team candidate contains a redirected artifact.')
        if path.is_file(): result[relative.as_posix()]=plain(path)
    return result


def require_initialized_job(job):
    """Reject a retired/missing job before any ledger, state or dispatch writes."""
    job=plain(job); marker=plain(job/'.pipeline_job')
    if job.parent.name!='jobs' or not job.is_dir() or not marker.is_file():
        raise RuntimeError('The isolated pipeline no longer exists; no work may be started.')
    try:
        valid=marker.stat().st_size<=128 and marker.read_text(encoding='utf-8').strip()=='isolated-demo-v1'
    except OSError:
        raise RuntimeError('The isolated pipeline marker is unavailable; no work may be started.') from None
    if not valid: raise RuntimeError('The isolated pipeline marker is invalid.')
    from studio_pipeline_delete import reject_deleted_id
    reject_deleted_id(job.parent.parent,job.name)
    return job


@contextmanager
def pipeline_lease(job):
    """Take an existing job's OS lease; never recreate a deleted job directory."""
    job=require_initialized_job(job); lock=plain(job/'.team-owner.lock')
    try:
        handle=lock.open('a+b')
    except OSError:
        raise RuntimeError('The pipeline is unavailable or already has an owning runner.') from None
    with handle:
        try:
            if os.fstat(handle.fileno()).st_size==0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('This pipeline already has an owning runner.') from None
        try:
            require_initialized_job(job)
            yield
        finally:
            if os.name=='nt':
                handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)



def task_epoch(value):
    if isinstance(value,bool) or not isinstance(value,int) or value<0:
        raise RuntimeError('Task epoch must be a nonnegative host-managed integer.')
    return value


def replacement_epoch(plan,state,entries):
    """Only a recorded cancelled-task replacement opens a new count window."""
    old=task_epoch(state.get('applied_task_epoch',state.get('task_epoch',0)))
    new=task_epoch(plan.get('task_epoch',0))
    if new<old: raise RuntimeError('The saved task epoch is newer than this task.')
    cursor=old
    for entry in sorted(entries,key=lambda item:item['revision']):
        if entry.get('task_replaced') is not True: continue
        before=task_epoch(entry.get('previous_task_epoch'))
        after=task_epoch(entry.get('task_epoch'))
        if before!=cursor or after!=before+1 or 'task_epoch' not in entry.get('changed_fields',[]):
            raise RuntimeError('Task replacement history is inconsistent; invocation windows are retained.')
        cursor=after
    if cursor!=new:
        raise RuntimeError('A new task epoch requires a recorded task replacement and revision.')
    return old,new,new!=old


def task_attempts(row):
    attempts=row.get('attempts',0); baseline=row.get('task_epoch_attempt_baseline',0)
    if any(isinstance(value,bool) or not isinstance(value,int) or value<0 for value in (attempts,baseline)) or baseline>attempts:
        raise RuntimeError('Saved task invocation counters are inconsistent.')
    return attempts-baseline


def task_call_limits(config,row):
    """Native tags remain cumulative; only count ceilings gain the task baseline."""
    task_attempts(row)
    baseline=row.get('task_epoch_attempt_baseline',0)
    for key in ('max_claude_calls_per_job','max_codex_calls_per_stage','max_research_calls'):
        value=config.get(key)
        if isinstance(value,int) and not isinstance(value,bool) and value>0:
            config[key]=value+baseline

@dataclass
class MemberContext:
    module: object
    config: dict
    job: Path
    member: dict
    phase: str
    request_id: str | None = None


class V2Engine:
    def __init__(self,root,job,plan,*,approval=None,secrets=None,event_callback=None,
                 executor=None,preflight=None,checkpoint=None,qa=None,delivery=None):
        self.root=plain(root); self.job=plain(job)
        if self.root.name.casefold() in ('ai-studio','ai-studio-v2'):
            raise RuntimeError('Earlier generations are read-only; use the version 2.5 studio.')
        if not self.job.is_relative_to(self.root/'jobs') or not (self.job/'.pipeline_job').is_file():
            raise RuntimeError('A team requires one initialized pipeline inside this studio.')
        self.plan=json.loads(json.dumps(plan,allow_nan=False))
        task_epoch(self.plan.get('task_epoch',0))
        revision=self.plan.get('revision',0)
        if isinstance(revision,bool) or not isinstance(revision,int) or revision<0:
            raise RuntimeError('A plan revision must be a nonnegative integer.')
        if plan.get('schema')!='ai-studio-pipeline/v2' or plan.get('pipeline_id')!=self.job.name:
            raise RuntimeError('The frozen plan does not belong to this pipeline.')
        self.approval=dict(approval or {}); self.secrets=dict(secrets or {})
        self.allowed=set(self.approval.get('providers',[])) if self.approval.get('approved') is True else set()
        self.callback=event_callback; self.executor=executor; self.preflight_hook=preflight
        self.checkpoint_hook=checkpoint; self.qa_hook=qa; self.delivery_hook=delivery
        self.lock=threading.RLock(); self.child_lock=threading.RLock()
        self.stop=threading.Event(); self.children=set(); self.contexts={}
        self.path=self.job/'.team-state.json'; self.module_names=[]
        self.members={member['id']:{**member,'role':role} for role,spec in plan['roles'].items() for member in spec['members']}
        if len(self.members)!=sum(len(spec['members']) for spec in plan['roles'].values()):
            raise RuntimeError('Team member IDs must be unique throughout the pipeline.')
        explicit_members=self.approval.get('members')
        self.allowed_members=(set(explicit_members) if explicit_members is not None else
                              {member_id for member_id,member in self.members.items() if member['provider_id'] in self.allowed})
        if not self.allowed_members.issubset(self.members):
            raise RuntimeError('API approval names an unknown team member.')
        # Initialization can write a fresh ledger; hold the same short OS lease.
        with pipeline_lease(self.job):
            self.ledger=TeamBudgetLedger(self.job/'.team-budget.json',plan['total_budget_usd'],
                {role:spec['budget_usd'] for role,spec in plan['roles'].items()},
                {member_id:{'role':member['role'],'budget_usd':member['budget_usd']} for member_id,member in self.members.items()})
            approved=self.approval.get('job_budget_usd',plan['total_budget_usd'])
            self.approval_cap=approved
            self.state=read(self.path) if self.path.is_file() else self.initial_state()
            if self.state.get('pipeline_id')!=plan['pipeline_id']:
                raise RuntimeError('Saved progress belongs to a different pipeline.')
            self.state.setdefault('references',{}); self.state.setdefault('qa_repairs',0)
        self._lease_held=False

    @contextmanager
    def lease(self):
        with pipeline_lease(self.job):
            with self.lock: self._lease_held=True
            try:
                require_initialized_job(self.job)
                recover_plan_update(self.job,lease_held=True)
                yield
            finally:
                with self.lock: self._lease_held=False

    @contextmanager
    def write_lease(self):
        """Lifecycle writes reuse an active run lease or take a separate short lease."""
        with self.lock:
            if self._lease_held:
                require_initialized_job(self.job)
                yield
                return
        # Do not hold the state mutex during independent checkpoint hooks.
        with pipeline_lease(self.job):
            yield

    def billing(self,member):
        return self.plan['providers'][member['provider_id']]['billing']

    def budget_guard(self,member,*,budget_failure=False,required_amount=None):
        """Only proven metered USD exhaustion can create a budget request."""
        if self.billing(member)!='metered': return True
        row=self.state['members'][member['id']]; snapshot=self.ledger.snapshot()
        if snapshot.get('unknown_invocations') or snapshot.get('interrupted_invocations'):
            self.dump(self.job/'pause.request',{'reason':'Actual paid costs require reconciliation; no budget grant can replace this.'})
            row.update(status='PAUSED',error='Unknown or interrupted paid costs require real usage evidence.')
            return False
        scopes=[('member',snapshot['members'][member['id']]),('role',snapshot['roles'][member['role']]),('pipeline',snapshot)]
        exhausted=[]
        needed=Decimal(str(required_amount or 0))
        for name,stats in scopes:
            remaining=Decimal(str(stats['cap_usd']))-Decimal(str(stats['spent_usd']))
            if remaining<=0 or (budget_failure and needed>0 and remaining<needed): exhausted.append((name,remaining))
        if exhausted:
            names=', '.join(name for name,remaining in exhausted)
            reason='Actual '+names+' API USD budget is insufficient to continue this unfinished member.'
            suggestion=max(Decimal('.10'),needed-min(remaining for name,remaining in exhausted))
            request=record_budget_request(self.job,self.plan,member['id'],reason,snapshot=snapshot,
                suggested_increase_usd=suggestion,lease_held=self._lease_held)
            row.update(status='WAITING_BUDGET',budget_request_id=request['id'],budget_request_status=request['status'],error=reason)
            self.emit({'type':'budget_request','status':'WAITING_BUDGET','request_id':request['id'],
                       'activity':'API 预算不足，已提出具体申请；保存产物并等待人工批准，不自动追加费用。'},member)
            return False
        approved_remaining=Decimal(str(self.approval_cap))-Decimal(str(snapshot['spent_usd']))
        if approved_remaining<=0:
            row.update(status='WAITING_APPROVAL',error='The current-session cumulative API approval is exhausted; no member cap increase was requested.')
            return False
        if any(Decimal(str(stats['available_usd']))<=0 for name,stats in scopes):
            row.update(status='WAITING_RESERVATION',error='Other active invocations reserve the remaining API allowance; no USD increase is implied.')
            return False
        return True

    def initial_state(self):
        stages={}
        members={}
        for role,spec in self.plan['roles'].items():
            ids=[member['id'] for member in spec['members']]
            stage=ROLE_STAGE[role]
            stages[stage]={'name':stage,'role':role,'status':'DORMANT' if role in ('integration','research') else 'READY',
                           'member_ids':ids,'lead_id':ids[0] if ids else None,'curation_status':'DORMANT','conflicts':[]}
            for member in spec['members']:
                provider=self.plan['providers'][member['provider_id']]
                members[member['id']]={**member,'role':role,'stage':stage,
                    'model':member.get('model') or provider.get('model') or (self.plan.get('config',{}).get(provider['adapter']+'_model') if provider['adapter'] in ('codex','claude') else None),
                    'status':'DORMANT' if role in ('integration','research') else 'READY','phase':'production',
                    'attempts':0,'phase_attempts':{},'task_epoch_attempt_baseline':0,'task_epoch':self.plan.get('task_epoch',0),
                    'workspace':str(self.job/'.team'/stage/member['id']),
                    'summary':'','error':'','files':[]}
        stages['qa']={'name':'qa','role':None,'status':'DORMANT','attempts':0,'member_ids':[],'lead_id':None}
        return {'schema':'ai-studio-team-state/v2','pipeline_id':self.plan['pipeline_id'],'plan_revision':self.plan.get('revision',0),
                'applied_plan_revision':self.plan.get('revision',0),'plan_goal':self.plan['goal'],'status':'READY',
                'task_epoch':self.plan.get('task_epoch',0),'applied_task_epoch':self.plan.get('task_epoch',0),
                'updated_at':time.time(),'stages':stages,'members':members,'references':{},'qa_repairs':0}

    def safe(self,text):
        for value in self.secrets.values():
            if isinstance(value,str) and value:
                text=text.replace(value,'[REDACTED]').replace(json.dumps(value,ensure_ascii=False)[1:-1],'[REDACTED]').replace(json.dumps(value)[1:-1],'[REDACTED]')
        return text

    def dump(self,path,data):
        path=plain(path); path.parent.mkdir(parents=True,exist_ok=True)
        temporary=plain(path.with_name('.'+path.name+'-'+str(threading.get_ident())+'.tmp'))
        temporary.write_text(self.safe(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)),encoding='utf-8')
        os.replace(temporary,path)

    def emit(self,event,member=None):
        value={'type':'activity','pipeline_id':self.job.name,'job_id':self.job.name,'at':time.time(),**event}
        if member: value.update(member_id=member['id'],role=member['role'],stage=ROLE_STAGE[member['role']],provider_id=member['provider_id'])
        value.setdefault('text',value.get('activity',''))
        value=json.loads(self.safe(json.dumps(value,ensure_ascii=False,default=str)))
        with self.lock:
            with (self.job/'.studio-events.jsonl').open('a',encoding='utf-8') as output:
                output.write(json.dumps(value,ensure_ascii=False)+'\n')
        if self.callback: self.callback(value)

    def persist(self):
        with self.lock:
            self.state['updated_at']=time.time(); self.state['budget']=self.ledger.snapshot()
            self.state['budget_requests']=load_budget_requests(self.job)['requests']
            self.dump(self.path,self.state)

    def pause_requested(self):
        return any((self.job/name).is_file() for name in ('pause.request','.controlpause'))

    def _copy(self,source,destination,*,skip_source=False):
        destination=plain(destination); destination.mkdir(parents=True,exist_ok=True)
        for relative,path in project_files(source).items():
            if skip_source and relative in ('source.blend','source.blend1'): continue
            target=plain(destination/relative); target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,target)

    def workspace(self,member,request_id=None):
        base=self.job/'.team'/ROLE_STAGE[member['role']]/member['id']
        if request_id: base=base/'requests'/request_id
        plain(base)
        if not (base/'task.json').is_file():
            base.mkdir(parents=True,exist_ok=True)
            for folder in ('game','art','references','delivery'):
                self._copy(self.job/folder,base/folder,skip_source=member['role']=='vfx' and folder=='art')
            # The installed project-scoped Godot bridge recognizes this marker;
            # generation/team identity is separately recorded in task/plan/state.
            (base/'.pipeline_job').write_text('isolated-demo-v1\n',encoding='utf-8')
            task=read(self.job/'task.json')
            task.update(kind=self.plan['kind'],goal=self.plan['goal'],job_id=member['id'],budget_usd=member['budget_usd'])
            self.dump(base/'task.json',task)
            seed={name:{relative:digest(path) for relative,path in project_files(base/name).items()} for name in ('game','art','references')}
            self.dump(base/'.seed.json',seed)
        # A free role/profile check can initialize this candidate before its
        # producers finish. Refresh only an untouched, undispatched integration
        # seed once both authoritative inputs are ready. Never replace agent or
        # user progress, invalidate a manual exchange, or reset usage/attempts.
        if (member['role']=='integration' and not request_id
                and all(self.state['stages'][ROLE_STAGE[role]]['status']=='DONE' for role in ('gameplay','vfx'))
                and (base/'.seed.json').is_file()
                and not any((base/'game'/'.pipeline').glob('gpt_integrate-*'))
                and not (base/'.adapter-state'/'tasks'/'gpt_integrate'/'task.json').exists()):
            seed=read(base/'.seed.json')
            current={name:{relative:digest(path) for relative,path in project_files(base/name).items()} for name in ('game','art','references')}
            if current==seed:
                for folder in ('game','art','references'):
                    self._copy(self.job/folder,base/folder)
                refreshed={name:{relative:digest(path) for relative,path in project_files(base/name).items()} for name in ('game','art','references')}
                self.dump(base/'.seed.json',refreshed)
                if refreshed!=seed:
                    self.emit({'type':'handoff','status':'READY','activity':'已将真实制作成果交接到尚未执行的整合工作区；费用与尝试记录保留。'},member)
        task=read(base/'task.json')
        task.update(goal=self.plan['goal'],assignment=member.get('assignment',''),plan_revision=self.plan.get('revision',0),budget_usd=member['budget_usd'],task_epoch=self.plan.get('task_epoch',0))
        self.dump(base/'task.json',task)
        self.state['members'][member['id']]['workspace']=str(base)
        return base

    def context(self,member,phase,request_id=None):
        base=self.workspace(member,request_id); key=str(base)
        if key in self.contexts:
            ctx=self.contexts[key]; ctx.phase=phase; return ctx
        name='_studio_v2_member_'+hashlib.sha256((str(base)+str(os.getpid())).encode()).hexdigest()
        spec=importlib.util.spec_from_file_location(name,self.root/'pipeline.py')
        module=importlib.util.module_from_spec(spec); sys.modules[name]=module; spec.loader.exec_module(module)
        self.module_names.append(name)
        module.CONTROL=base/'.control'; module.STOP=self.stop; module.CHILDREN=self.children; module.CHILD_LOCK=self.child_lock
        module.SECRETS.update({'provider:'+key:value for key,value in self.secrets.items() if isinstance(value,str)})
        if self.secrets.get('claude-code'): module.SECRETS['claude']=self.secrets['claude-code']
        selected=self.plan['providers'][member['provider_id']]
        if selected['adapter']=='openrouter-research' and self.secrets.get(member['provider_id']):
            module.SECRETS['research']=self.secrets[member['provider_id']]
        def own_job(identifier):
            if identifier not in (base.name,member['id'],self.job.name):
                raise RuntimeError('The member attempted to authorize a different workspace.')
            return plain(base)
        module.job_path=own_job
        paid=[value for value in self.members.values() if self.plan['providers'][value['provider_id']]['billing']=='metered']
        role_paid=[value for value in paid if value['role']==member['role']]
        fair_total=min(Decimal(str(self.plan['total_budget_usd'])),Decimal(str(self.approval_cap)))/max(1,len(paid))
        fair_role=Decimal(str(self.plan['roles'][member['role']]['budget_usd']))/max(1,len(role_paid))
        cfg={**self.plan.get('config',{}),'provider_settings':self.plan.get('provider_settings',{}),
             '_provider_secrets':self.secrets,'_mcp_environment':{key.split(':',1)[1]:value for key,value in self.secrets.items() if key.startswith('mcp-variable:')},
             '_task_kind':self.plan['kind'],'godot_allowed_project':str(base/'game'),'godot_preapprove_launch':True,
             'worker_budget_usd_per_job':self.plan['total_budget_usd'],'claude_budget_usd_per_job':member['budget_usd'],
             '_v2_budget_view':self.ledger.bind(member['role'],member['id'],workspace_root=base,approval_cap=self.approval_cap),
             '_v2_call_cap':min(fair_total,fair_role,Decimal(str(member['budget_usd'])),Decimal(str(self.plan['roles'][member['role']]['budget_usd'])),
                 Decimal(str(self.plan.get('config',{}).get('worker_budget_usd_per_call',member['budget_usd']))))}
        task_call_limits(cfg,self.state['members'][member['id']])
        cfg['_task_epoch']=self.plan.get('task_epoch',0)
        # Profiles are member-specific and always outside deliverables. Only the
        # existing opaque ChatGPT auth file is read, never its config/plugins.
        runtime=studio_blender.runtime_directory(self.root,self.plan.get('config',{}))
        profiles=runtime/'profiles'/self.job.name/member['id']
        if request_id: profiles=profiles/request_id
        auth_source=Path(self.plan.get('config',{}).get('profiles_dir',str(module.PROFILES)))/'codex'/'auth.json'
        cfg['profiles_dir']=str(profiles)
        adapter_runtime.private_profile_path(module,cfg,profiles/'codex'/'auth.json')
        if auth_source.is_file():
            module.ensure_plain_path(auth_source)
            target=profiles/'codex'/'auth.json'; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(auth_source,target)
        provider_specs=json.loads(json.dumps(self.plan['providers']))
        if member.get('model'): provider_specs[member['provider_id']]['model']=member['model']
        routes={role:(role_spec['members'][0]['provider_id'] if role!='research' else list(dict.fromkeys(m['provider_id'] for m in role_spec['members'])))
                for role,role_spec in self.plan['roles'].items() if role_spec['members']}
        routes[member['role']]=[member['provider_id']] if member['role']=='research' else member['provider_id']
        self.dump(base/'.workers.json',{'providers':provider_specs,'role_routes':routes})
        cfg['workers_file']=str(base/'.workers.json')
        frozen_mcp=self.plan.get('mcp',[])
        document=({'mcpServers':{entry['id']:{key:value for key,value in entry.items() if key!='id'} for entry in frozen_mcp}}
                  if isinstance(frozen_mcp,list) else frozen_mcp)
        self.dump(base/'mcp.local.json',document); cfg['mcp_file']=str(base/'mcp.local.json')
        module.CONTROL.mkdir(parents=True,exist_ok=True)
        for filename,data in (('empty.mcp.json',{'mcpServers':{}}),('result.schema.json',module.SCHEMA),('research.schema.json',module.RESEARCH_SCHEMA)):
            module.dump(module.CONTROL/filename,data)
        studio_tasks.install(module,cfg,base)
        module.APPROVE_PAID=member['provider_id'] in self.allowed and member['id'] in self.allowed_members
        from studio_v2_runner import member_process
        module.process=lambda args,cwd,env,**kwargs:member_process(module,args,cwd,env,
            emit=lambda event:self.emit(event,member),**kwargs)
        ctx=MemberContext(module,cfg,base,member,phase,request_id); self.contexts[key]=ctx
        return ctx

    def preflight(self,ctx):
        provider=adapter_runtime.provider_for(ctx.module,ctx.config,ctx.job,ROLE_STAGE[ctx.member['role']])
        if provider.billing=='metered' and (ctx.member['provider_id'] not in self.allowed or ctx.member['id'] not in self.allowed_members):
            raise RuntimeError('This member has no explicit current-session API approval.')
        if self.preflight_hook:
            self.preflight_hook(ctx); return
        if provider.adapter=='codex':
            ctx.module.check_auth(adapter_runtime.native_config(ctx.module,ctx.config,provider))
        elif provider.adapter in ('claude','kimi'):
            adapter_runtime.native_config(ctx.module,ctx.config,provider,allow_default_prompt=True)
        elif provider.adapter=='openrouter-research' and not ctx.module.SECRETS.get('research'):
            raise RuntimeError('The selected researcher needs its own private credential.')
        if ctx.member['role']=='vfx' and self.plan['kind']!='software' and provider.adapter in ('codex','claude','kimi'):
            identity='v2-'+hashlib.sha256((self.job.name+':'+ctx.member['id']).encode()).hexdigest()[:32]
            port=studio_blender.ensure_instance(self.root,identity,ctx.config,lambda event:self.emit(event,ctx.member),job_dir=ctx.job)
            ctx.config.update(blender_port=port,blender_mcp_args=['--host','127.0.0.1','--port',str(port)],_v2_blender_identity=identity)

    def execute(self,ctx,attempt):
        if self.pause_requested() or self.stop.is_set():
            raise RuntimeError('Paused before member dispatch; no model was started.')
        extra=('\nTEAM ASSIGNMENT (data): '+ctx.member.get('assignment','')+'\n'
               'Work only in this member candidate workspace. Preserve saved progress; never edit another member or canonical pipeline.\n')
        if self.plan.get('task_epoch',0):
            extra+='CURRENT TASK EPOCH: '+str(self.plan['task_epoch'])+'. Work on the newly saved task; cancelled-task calls and artifacts are historical, and cumulative fees/attempt tags are retained.\n'
        if ctx.phase=='curation':
            extra+=('\nCURATION: inspect team-handoffs/curation.json and each successful helper contribution and changeset. '
                    'Combine useful work into the authoritative candidate, resolve conflicts explicitly, and write TEAM_MERGE_REPORT.md '
                    'listing contributions used, rejected, and reasons for each conflict resolution. Do not silently replace helper work. '
                    'A complete usable output is required; passing summaries cannot substitute actual files.\n')
            folder='art' if ctx.member['role']=='vfx' else 'game'
            manifest=read(ctx.job/folder/'team-handoffs'/'curation.json')
            # Actual helper metadata is sent to the lead, with bounded text
            # excerpts; binary resources remain available as real local files.
            extra+='ACTUAL CONTRIBUTIONS:\n'+json.dumps(manifest,ensure_ascii=False)+'\n'
            remaining=32768
            for contribution in manifest['contributions']:
                if contribution['member_id']==ctx.member['id']: continue
                for relative in list(contribution['changed'])[:12]:
                    path=plain(Path(contribution['workspace'])/relative)
                    if path.suffix.lower() not in ('.py','.gd','.js','.css','.html','.svg','.json','.md','.txt','.tscn') or remaining<=0: continue
                    text=path.read_text(encoding='utf-8',errors='replace')[:min(4096,remaining)]
                    extra+='HELPER '+contribution['member_id']+' FILE '+relative+':\n'+text+'\n'; remaining-=len(text)
            for filename in ('godot_import.log','godot_acceptance.log','godot_startup.log','software_tests.log','qa_report.json'):
                evidence=self.job/'delivery'/filename
                if evidence.is_file(): extra+='ACTUAL QA EVIDENCE '+filename+':\n'+evidence.read_text(encoding='utf-8',errors='replace')[-8000:]+'\n'
        ctx.config['_v2_prompt_extra']=extra
        lead_id=self.state['stages'][ROLE_STAGE[ctx.member['role']]]['lead_id']
        ctx.config['_v2_validate_candidate']=ctx.member['id']==lead_id
        if self.executor: return self.executor(ctx,attempt)
        return adapter_runtime.worker(ctx.module,ctx.config,ctx.job,ROLE_STAGE[ctx.member['role']],attempt)

    def limit(self,member,phase):
        policy=self.plan.get('policy',{})
        key='max_curation_attempts' if phase=='curation' else 'max_research_attempts' if member['role']=='research' else 'max_member_attempts'
        value=policy.get(key,2 if phase=='curation' else 4)
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=16:
            raise RuntimeError('Team attempts must have an explicit bounded limit between one and sixteen.')
        return value

    def apply_revision(self):
        current=self.plan.get('revision',0)
        previous=self.state.get('applied_plan_revision',self.state.get('plan_revision',0))
        if current<previous: raise RuntimeError('The saved plan is older than the applied member progress.')
        if current==previous:
            replacement_epoch(self.plan,self.state,[])
            return
        path=self.job/'.plan-revisions.json'
        history=read(path) if path.is_file() else {'entries':[]}
        entries=[entry for entry in history.get('entries',[]) if previous<entry['revision']<=current]
        if not entries or max(entry['revision'] for entry in entries)!=current:
            raise RuntimeError('Plan revision history is missing; old completion cannot certify a new goal.')
        old_epoch,new_epoch,new_task=replacement_epoch(self.plan,self.state,entries)
        affected=set(); specified=set(); goal_changed=False; research_changed=False
        for entry in entries:
            if entry.get('budget_only'): continue
            affected.update(entry['affected_stages']); specified.update(entry.get('affected_members',[]))
            goal_changed=goal_changed or 'goal' in entry.get('changed_fields',[])
            research_changed=research_changed or any(self.members.get(member_id,{}).get('role')=='research' for member_id in entry.get('affected_members',[]))
        if new_task:
            affected.update(ROLE_STAGE.values()); affected.add('qa'); specified.update(self.members)
        if goal_changed or research_changed: affected.add('research')
        archive=self.job/'checkpoint'/'revisions'/('revision-'+str(previous)+'-to-'+str(current))
        if affected:
            archive.mkdir(parents=True,exist_ok=True)
            if not (archive/'team-state.json').exists(): self.dump(archive/'team-state.json',self.state)
            for folder in ('game','art','references','handoffs'):
                if not (archive/folder).exists(): self._copy(self.job/folder,archive/folder)
            # Unpublished partial candidates are history too. Keep their real
            # bytes before the revised task is allowed to edit the working copy.
            for row in self.state['members'].values():
                base=plain(row['workspace'])
                if not base.is_relative_to(self.job/'.team'): raise RuntimeError('A member candidate escapes this pipeline.')
                for folder in ('game','art','references'):
                    target=archive/base.relative_to(self.job)/folder
                    if not target.exists(): self._copy(base/folder,target)
            delivery=self.job/'delivery'; old_delivery=archive/'delivery'; old_delivery.mkdir(exist_ok=True)
            for source in list(delivery.iterdir()):
                plain(source)
                if source.is_symlink(): raise RuntimeError('Redirected historical delivery is refused.')
                if source.name=='DELIVERY_STALE.json': continue
                target=old_delivery/source.name
                if not target.exists(): shutil.move(str(source),str(target))
            self.dump(delivery/'DELIVERY_STALE.json',{'status':'STALE','previous_revision':previous,'plan_revision':current,
                'reason':'Goal or assignment changed; new work and actual QA are required.','archive':str(archive)})
            for role,spec in self.plan['roles'].items():
                stage_id=ROLE_STAGE[role]
                if stage_id not in affected: continue
                stage=self.state['stages'][stage_id]
                stage.update(status='DORMANT' if role in ('integration','research') else 'READY',curation_status='DORMANT',conflicts=[])
                chosen=(set(stage['member_ids']) if new_task or goal_changed or role=='integration' or not (specified & set(stage['member_ids']))
                        else specified & set(stage['member_ids']))
                for member_id in chosen:
                    row=self.state['members'][member_id]; base=Path(row['workspace'])
                    folder='art' if role=='vfx' else 'references' if role=='research' else 'game'
                    row['revision_artifact_baseline']={name:digest(file) for name,file in project_files(base/folder).items()}
                    row['phase_baselines']=dict(row['phase_attempts'])
                    if new_task:
                        row['task_epoch_attempt_baseline']=row['attempts']
                        row['task_epoch']=new_epoch
                        row.pop('request_id',None)
                    row.update(status='DORMANT' if role in ('integration','research') else 'READY',phase='production',
                        revision_required=current,plan_revision=current,summary='',error='',files=[])
                    row.pop('result',None); row.pop('production_result',None); row.pop('reference_request',None)
                    task_file=base/'task.json'
                    if task_file.is_file():
                        task=read(task_file); task.update(goal=self.plan['goal'],assignment=self.members[member_id]['assignment'],plan_revision=current)
                        self.dump(task_file,task)
                    if base.exists(): self.dump(base/'.seed.json',{name:{relative:digest(file) for relative,file in project_files(base/name).items()} for name in ('game','art','references')})
                    packets=base/'.adapter-state'/'tasks'
                    if packets.exists():
                        retired=base/'.adapter-state'/'revisions'/str(current)/'tasks'
                        retired.parent.mkdir(parents=True,exist_ok=True)
                        if not retired.exists(): shutil.move(str(packets),str(retired))
                lead=stage.get('lead_id')
                if lead:
                    row=self.state['members'][lead]
                    row.setdefault('phase_baselines',{})['curation']=row['phase_attempts'].get('curation',0)
            if 'qa' in affected:
                qa=self.state['stages']['qa']; qa['status']='DORMANT'; qa.pop('error',None)
                self.state['qa_repair_baseline']=self.state.get('qa_repairs',0)
            if new_task:
                for request in self.state['references'].values():
                    request.setdefault('task_epoch',old_epoch)
                    request['status']='STALE'
                    for run in request.get('runs',{}).values(): run['status']='STALE'
            elif goal_changed or research_changed: self.state['references']={}
            self.state['status']='PAUSED' if self.pause_requested() else 'READY'
        for member_id,member in self.members.items():
            for key in ('assignment','budget_usd','required'): self.state['members'][member_id][key]=member[key]
        self.contexts.clear()
        self.state.update(plan_revision=current,applied_plan_revision=current,plan_goal=self.plan['goal'],
                          task_epoch=new_epoch,applied_task_epoch=new_epoch)
        self.persist()
        self.emit({'type':'plan_revision','plan_revision':current,'activity':'预算修订已应用，已完成成果保留。' if not affected else '目标或分工已修订；历史保留，相关制作、整合和验收重新执行。'})

    def _submit(self,pool,futures,member,phase='production',request_id=None):
        if self.pause_requested() or self.stop.is_set(): return False
        row=self.state['members'][member['id']]
        if request_id:
            request=self.state['references'].get(request_id,{})
            if request.get('task_epoch',0)!=self.plan.get('task_epoch',0) or request.get('status') in ('STALE','CANCELLED'): return False
        external=row['status']=='WAITING_EXTERNAL'
        if external:
            base=Path(row['workspace'])
            if not adapter_runtime.external_ready(base,ROLE_STAGE[member['role']]): return False
        elif row['status']!='READY': return False
        used=row['phase_attempts'].get(phase,0)
        baseline=row.get('phase_baselines',{}).get(phase,0)
        if not external and (used-baseline>=self.limit(member,phase) or task_attempts(row)>=16):
            row.update(status='BUDGET_STOP',error='Member phase invocation-count limit reached.'); return False
        try:
            ctx=self.context(member,phase,request_id)
            self.preflight(ctx)
        except Exception as error:
            row.update(status='WAITING_CONNECTION',error=self.safe(str(error)))
            self.emit({'type':'member','status':row['status'],'activity':'成员接入等待；其他独立成员可继续。','text':row['error']},member)
            return False
        # A pause may arrive while auth/Blender readiness checks are in progress.
        if self.pause_requested() or self.stop.is_set(): return False
        if not external and not self.budget_guard(member): return False
        if not external:
            row['attempts']+=1; row['phase_attempts'][phase]=used+1
        row.update(status='RUNNING',phase=phase,error='',request_id=request_id)
        self.emit({'type':'member','status':'RUNNING','phase':phase,'activity':'成员开始真实工作。'},member)
        self.persist()
        future=pool.submit(self.execute,ctx,row['attempts']); futures[future]=(member,ctx)
        if request_id:
            self.state['references'][request_id]['runs'][member['id']]={'status':'RUNNING','workspace':str(ctx.job),'attempt':row['attempts']}
        return True

    def reference_request(self,member,query):
        query=query.strip()[:4000]
        if not query: raise RuntimeError('A reference request must contain a precise query.')
        epoch=self.plan.get('task_epoch',0)
        identity=query if epoch==0 else json.dumps([epoch,query],ensure_ascii=False)
        request_id=hashlib.sha256(identity.encode()).hexdigest()[:24]
        requests=self.state['references']
        if request_id not in requests:
            requests[request_id]={'query':query,'status':'READY','requesters':[],'runs':{},'created_at':time.time(),'task_epoch':epoch}
        request=requests[request_id]
        if member['id'] not in request['requesters']: request['requesters'].append(member['id'])
        row=self.state['members'][member['id']]
        row.update(status='WAITING_REFERENCE',reference_request=request_id)
        if request['status']=='DONE': self._deliver_references(request_id)
        self.emit({'type':'reference_request','request_id':request_id,'activity':'成员提出资料需求；按需安排研究团队。','text':query},member)

    def _finish(self,future,member,ctx):
        row=self.state['members'][member['id']]
        completed_result=None; caught_error=None
        try:
            result=future.result()
            completed_result=result
            # Manual adapters return a controller-only wait status rather than
            # a model completion; validate its ordinary fields with a neutral status.
            ctx.module.validate_result({**result,'status':'blocked'} if result.get('status')=='waiting_external' else result)
            for relative in result['files']:
                workspace=ctx.job/('art' if member['role']=='vfx' else 'references' if member['role']=='research' else 'game')
                if not ctx.module.within(workspace,relative).is_file(): raise RuntimeError('A member declared an artifact that does not exist.')
            row.update(summary=result['summary'],files=result['files'],result=result)
            if result['status']=='needs_reference' and member['role']!='research':
                self.reference_request(member,result['resource_query'])
            elif result['status']=='waiting_external': row['status']='WAITING_EXTERNAL'
            elif result['status']=='done':
                if member['role']!='research' and row.get('revision_required')==self.plan.get('revision',0):
                    folder='art' if member['role']=='vfx' else 'references' if member['role']=='research' else 'game'
                    ignored={'TEAM_MERGE_REPORT.md','HANDOFF.md','references.json','ui_ready.json','vfx_ready.json'}
                    artifacts={name:digest(path) for name,path in project_files(ctx.job/folder).items() if Path(name).name not in ignored}
                    before={name:value for name,value in row.get('revision_artifact_baseline',{}).items() if Path(name).name not in ignored}
                    if artifacts==before:
                        raise RuntimeError('The revised goal/assignment produced no changed artifact; old completion cannot certify the new revision.')
                    row['completed_revision']=self.plan.get('revision',0); row.pop('revision_required',None)
                row['status']='DONE'
                if ctx.phase=='production': row['production_result']=result
                if ctx.phase=='curation':
                    report=ctx.job/('art' if member['role']=='vfx' else 'game')/'TEAM_MERGE_REPORT.md'
                    if not report.is_file() or not report.read_text(encoding='utf-8').strip():
                        raise RuntimeError('Curation requires an actual contribution/conflict report.')
                    content=report.read_text(encoding='utf-8')
                    handoff=read(report.parent/'team-handoffs'/'curation.json')
                    if any(item['member_id'] not in content for item in handoff['contributions'] if item['member_id']!=member['id']):
                        raise RuntimeError('The merge report omitted a successful helper contribution.')
                    if any(item['path'] not in content for item in handoff['conflicts']):
                        raise RuntimeError('The merge report omitted a real file conflict.')
                    self.state['stages'][ROLE_STAGE[member['role']]]['curation_status']='DONE'
            else: row['status']='BLOCKED'
        except Exception as error:
            caught_error=error
            row.update(status='PAUSED' if self.pause_requested() else 'FAILED',error=self.safe(str(error)))
            if any(word in str(error).lower() for word in ('unknown cost','cost is unknown','unknown costs','cost reconciliation','unproven pending')):
                self.dump(self.job/'pause.request',{'reason':'This pipeline requires real paid-cost reconciliation.','at':time.time()})
        if row['status'] not in ('DONE','WAITING_EXTERNAL','PAUSED') and self.billing(member)=='metered':
            folder='art' if member['role']=='vfx' else 'references' if member['role']=='research' else 'game'
            usage=ctx.job/folder/'.pipeline'/(ROLE_STAGE[member['role']]+'-'+str(row['attempts'])+'.usage.json')
            metadata=read(usage) if usage.is_file() else {}
            sdk_budget_stop=metadata.get('subtype')=='error_max_budget_usd'
            excluded=('connection','authentication','api key','rate limit','quota','subscription','max_turns','max turns','invocation-count','call count')
            description=str(caught_error or (completed_result or {}).get('summary','')).lower()
            budget_eligible=(sdk_budget_stop or (caught_error is None and completed_result is not None)) and not any(token in description for token in excluded)
            snapshot=self.ledger.snapshot()
            if budget_eligible or snapshot.get('unknown_invocations') or snapshot.get('interrupted_invocations'):
                self.budget_guard(member,budget_failure=sdk_budget_stop,required_amount=metadata.get('cli_budget_usd'))
        if ctx.request_id:
            self.state['references'][ctx.request_id]['runs'][member['id']]={
                'status':row['status'],'workspace':str(ctx.job),'attempt':row['attempts'],
                'result':row.get('result'),'error':row.get('error','')}
        self.emit({'type':'member','status':row['status'],'phase':ctx.phase,'activity':row.get('summary') or row.get('error') or row['status']},member)
        self.persist()

    def changeset(self,member):
        row=self.state['members'][member['id']]; base=Path(row['workspace'])
        folder='art' if member['role']=='vfx' else 'game'
        old=read(base/'.seed.json').get(folder,{})
        current={relative:digest(path) for relative,path in project_files(base/folder).items()}
        return {'member_id':member['id'],'provider_id':member['provider_id'],'assignment':member.get('assignment',''),
                'workspace':str(base/folder),'summary':row.get('summary',''),
                'changed':{name:value for name,value in current.items() if old.get(name)!=value},
                'deleted':sorted(set(old)-set(current))}

    def _handoffs(self,role):
        spec=self.plan['roles'][role]; stage=self.state['stages'][ROLE_STAGE[role]]
        lead=spec['members'][0]; lead_base=Path(self.state['members'][lead['id']]['workspace'])
        folder='art' if role=='vfx' else 'game'
        destination=lead_base/folder/'team-handoffs'; destination.mkdir(parents=True,exist_ok=True)
        changes=[]
        for member in spec['members']:
            row=self.state['members'][member['id']]
            if row['status']!='DONE' and not (member['id']==lead['id'] and row.get('production_result')): continue
            change=self.changeset({**member,'role':role}); changes.append(change)
            change['curator_path']='.' if member['id']==lead['id'] else 'team-handoffs/'+member['id']+'/'+folder
            archive=self.job/'handoffs'/role/member['id']
            if archive.exists():
                plain(archive)
                if not archive.is_relative_to(self.job/'handoffs'): raise RuntimeError('Handoff archive escapes this pipeline.')
                shutil.rmtree(archive)
            self._copy(Path(change['workspace']),archive/folder)
            self.dump(archive/'changeset.json',change)
            if member['id']!=lead['id']:
                helper=plain(destination/member['id'])
                if helper.exists(): shutil.rmtree(helper)
                self._copy(Path(change['workspace']),destination/member['id']/folder)
                self.dump(destination/member['id']/'changeset.json',change)
        conflicts=[]
        all_names={name for change in changes for name in (*change['changed'],*change['deleted'])}
        for name in sorted(all_names):
            touched=[{'member_id':change['member_id'],'sha256':change['changed'].get(name),'deleted':name in change['deleted']}
                     for change in changes if name in change['changed'] or name in change['deleted']]
            if len({(item['sha256'],item['deleted']) for item in touched})>1:
                conflicts.append({'path':name,'candidates':touched})
        stage['conflicts']=conflicts
        report={'role':role,'lead_id':lead['id'],'contributions':changes,'conflicts':conflicts,
                'merge_policy':'Lead curator must inspect contributions; helper artifacts are never silently overlaid.'}
        self.dump(destination/'curation.json',report); self.dump(self.job/'handoffs'/role/'curation.json',report)

    def _publish(self,role):
        stage=self.state['stages'][ROLE_STAGE[role]]; lead=self.state['members'][stage['lead_id']]
        folder='art' if role=='vfx' else 'game'
        source=Path(lead['workspace'])/folder; target=self.job/folder
        source_files=project_files(source)
        for relative,path in project_files(target).items():
            if relative not in source_files: plain(path).unlink()
        self._copy(source,target)
        stage['status']='DONE'; stage['published_at']=time.time()
        if stage['curation_status']=='DORMANT': stage['curation_status']='NOT_REQUIRED'
        for member_id in stage['member_ids']:
            member=self.state['members'][member_id]
            if member['status']!='DONE' and not member['required']:
                member['excluded_from_merge']=True
        self.dump(self.job/'handoffs'/role/'published.json',{'lead_id':lead['id'],
            'source':str(source),'files':{relative:digest(path) for relative,path in source_files.items()},
            'curation_status':stage['curation_status'],'conflicts':stage['conflicts']})
        self.emit({'type':'stage','stage':ROLE_STAGE[role],'status':'DONE','activity':'团队候选已明确合并并交接。'})

    def advance_roles(self):
        for role in ('gameplay','vfx','integration'):
            spec=self.plan['roles'][role]; stage=self.state['stages'][ROLE_STAGE[role]]
            if stage['status']=='DONE': continue
            if role=='integration' and not all(self.state['stages'][ROLE_STAGE[dep]]['status']=='DONE' for dep in ('gameplay','vfx')): continue
            if role=='integration' and stage['status']=='DORMANT':
                stage['status']='READY'
                for member in spec['members']: self.state['members'][member['id']]['status']='READY'
            rows=[self.state['members'][member['id']] for member in spec['members']]
            if any(row['status']=='RUNNING' for row in rows): stage['status']='RUNNING'; continue
            if not rows: stage['status']='BLOCKED'; continue
            if rows[0]['status']!='DONE' or any(row['required'] and row['status']!='DONE' for row in rows):
                stage['status']='READY' if any(row['status']=='READY' for row in rows) else 'WAITING'; continue
            helpers=[row for row in rows[1:] if row['status']=='DONE']
            # Do not publish while an optional member is still actively eligible.
            if any(row['status'] in ('READY','RUNNING') for row in rows[1:]): continue
            if helpers and stage['curation_status']!='DONE':
                if stage['curation_status']=='DORMANT':
                    self._handoffs(role); stage['curation_status']='READY'
                    rows[0].update(status='READY',phase='curation')
                stage['status']='READY'; continue
            self._publish(role)

    def _merge_references(self,request_id):
        request=self.state['references'][request_id]
        completed=[(member_id,run) for member_id,run in request['runs'].items() if run['status']=='DONE']
        if not completed: return False
        members=self.plan['roles']['research']['members']
        if any(member['required'] and request['runs'].get(member['id'],{}).get('status')!='DONE' for member in members): return False
        if any(request['runs'].get(member['id'],{}).get('status') in ('READY','RUNNING') for member in members): return False
        sources=[]; seen=set(); contributions=[]
        for member_id,run in completed:
            directory=Path(run['workspace'])/'references'
            target=self.job/'references'/'team'/request_id/member_id; self._copy(directory,target)
            raw=read(directory/'references.json')
            contributions.append({'member_id':member_id,'raw_file':str(target/'references.json'),'summary':raw.get('summary','')})
            inspected=list(raw.get('sources',[]))
            for annotation in raw.get('annotations',[]):
                if isinstance(annotation,dict):
                    citation=annotation.get('url_citation',annotation)
                    if isinstance(citation,dict) and citation.get('url'): inspected.append(citation)
            for source in inspected:
                if not isinstance(source,dict): continue
                url=source.get('url','')
                if not isinstance(url,str) or not url.startswith(('https://','http://')) or url in seen: continue
                seen.add(url)
                sources.append({**source,'url':url,'license_status':'unknown','research_member':member_id,
                                'video_contents_verified':False,'download_performed':False})
        merged={'query':request['query'],'sources':sources,'contributions':contributions,
                'video_contents_verified':False,'license_clearance':'not_granted_by_this_pipeline','download_performed':False}
        target=self.job/'references'/('merged-'+request_id+'.json'); self.dump(target,merged)
        request.update(status='DONE',merged_file=str(target)); self._deliver_references(request_id)
        self.emit({'type':'research','stage':'research','request_id':request_id,'status':'DONE','activity':'研究团队实际来源与原始答复已合并；未暗示下载、观看或版权许可。'})
        return True

    def _deliver_references(self,request_id):
        request=self.state['references'][request_id]
        if request.get('status')!='DONE' or request.get('task_epoch',0)!=self.plan.get('task_epoch',0): return
        for member_id in request['requesters']:
            row=self.state['members'][member_id]; base=Path(row['workspace'])
            working=base/('art' if row['role']=='vfx' else 'game')
            bundle=working/'research-materials'/request_id
            # Give the requester actual files inside its own workspace, as well as citations.
            self._copy(self.job/'references'/'team'/request_id,bundle)
            merged=read(request['merged_file'])
            for contribution in merged.get('contributions',[]):
                contribution['raw_file']=str(bundle/contribution['member_id']/'references.json')
                contribution['artifact_directory']=str(bundle/contribution['member_id'])
            self.dump(base/'art'/'references.json',merged)
            self.dump(base/'game'/'references.json',merged)
            self.dump(base/'references'/'references.json',merged)
            if row['status']=='WAITING_REFERENCE': row['status']='READY'

    def submit_research(self,pool,futures):
        roster=self.plan['roles']['research']['members']
        for request_id,request in self.state['references'].items():
            if request.get('task_epoch',0)!=self.plan.get('task_epoch',0) or request['status'] in ('DONE','STALE','CANCELLED'): continue
            if not roster:
                request['status']='WAITING_CONNECTION'; continue
            for raw in roster:
                member={**raw,'role':'research'}; row=self.state['members'][member['id']]
                run=request['runs'].get(member['id'])
                if run and run['status'] in ('DONE','FAILED','BLOCKED','BUDGET_STOP','WAITING_CONNECTION','WAITING_BUDGET','WAITING_APPROVAL'): continue
                if row['status']=='RUNNING' or (row['status']=='WAITING_EXTERNAL' and row.get('request_id')!=request_id): continue
                if run and run['status']=='WAITING_EXTERNAL': row['status']='WAITING_EXTERNAL'
                else: row['status']='READY'
                base=self.workspace(member,request_id)
                self.dump(base/'references'/'request.json',{'query':request['query']+'\nAssigned research focus: '+member.get('assignment','')+
                    '\nOther researchers have separate assignments. Supply actual inspected sources; do not claim video viewing, downloads or license clearance.'})
                self._submit(pool,futures,member,'research',request_id)
                if row['status'] in ('WAITING_CONNECTION','WAITING_BUDGET','WAITING_APPROVAL','WAITING_RESERVATION'):
                    request['runs'][member['id']]={'status':row['status'],'workspace':str(base),'error':row.get('error','')}
            self._merge_references(request_id)
        stage=self.state['stages']['research']
        current=[request for request in self.state['references'].values() if request.get('task_epoch',0)==self.plan.get('task_epoch',0) and request['status'] not in ('STALE','CANCELLED')]
        pending=[request for request in current if request['status']!='DONE']
        active=any(run['status']=='RUNNING' for request in pending for run in request['runs'].values())
        stage['status']='RUNNING' if active else 'WAITING' if pending else 'DONE' if current else 'DORMANT'

    def acceptance(self):
        row=self.state['stages']['qa']
        if self.state['stages']['gpt_integrate']['status']!='DONE' or row['status']=='DONE': return
        row['status']='RUNNING'; row['attempts']+=1; self.persist()
        lead=self.members[self.state['stages']['gpt_integrate']['lead_id']]
        # A distinct module contract verifies the canonical project, never a helper summary.
        qa_ctx=self._canonical_context()
        try:
            if self.qa_hook: self.qa_hook(self,qa_ctx)
            else: qa_ctx.module.qa(qa_ctx.config,self.job)
            if self.pause_requested(): row['status']='PAUSED'; return
            if self.delivery_hook: self.delivery_hook(self,qa_ctx)
            else: qa_ctx.module.deliver(qa_ctx.config,self.job)
            stale=self.job/'delivery'/'DELIVERY_STALE.json'
            plain(stale).unlink(missing_ok=True)
            self.dump(self.job/'delivery'/'DELIVERY_CURRENT.json',{'plan_revision':self.plan.get('revision',0),'goal':self.plan['goal'],'qa':'pass','packaging':'complete'})
            row['status']='DONE'; self.state['status']='DONE'
            self.emit({'type':'stage','stage':'qa','status':'DONE','activity':'真实机器验收与交付打包均完成。'})
        except Exception as error:
            row.update(status='FAILED',error=self.safe(str(error)))
            policy=self.plan.get('policy',{})
            limit=min(2,int(policy.get('max_revisions',policy.get('max_retries',2))))
            if policy.get('auto_retry',False) and self.state['qa_repairs']-self.state.get('qa_repair_baseline',0)<limit and not self.pause_requested():
                self.state['qa_repairs']+=1
                leader=self.state['members'][lead['id']]
                leader.update(status='READY',phase='curation')
                stage=self.state['stages']['gpt_integrate']; stage.update(status='READY',curation_status='READY')
                self._handoffs('integration')
                row['status']='DORMANT'
                self.emit({'type':'repair','stage':'gpt_integrate','member_id':lead['id'],'activity':'真实验收失败，按有限次数交回原整合负责人修复。','text':str(error)})
        self.persist()

    def _canonical_context(self):
        key='canonical'
        if key in self.contexts: return self.contexts[key]
        name='_studio_v2_qa_'+hashlib.sha256(str(self.job).encode()).hexdigest()
        spec=importlib.util.spec_from_file_location(name,self.root/'pipeline.py'); module=importlib.util.module_from_spec(spec)
        sys.modules[name]=module; spec.loader.exec_module(module); self.module_names.append(name)
        module.CONTROL=self.job/'.control'; module.STOP=self.stop; module.CHILDREN=self.children; module.CHILD_LOCK=self.child_lock
        module.SECRETS.update({'provider:'+key:value for key,value in self.secrets.items() if isinstance(value,str)})
        cfg={**self.plan.get('config',{}),'_task_kind':self.plan['kind']}
        studio_tasks.install(module,cfg,self.job)
        from studio_v2_runner import member_process
        module.process=lambda args,cwd,env,**kwargs:member_process(module,args,cwd,env,emit=lambda event:self.emit({**event,'stage':'qa'}),**kwargs)
        ctx=MemberContext(module,cfg,self.job,{},'qa'); self.contexts[key]=ctx; return ctx

    def checkpoint(self,*,save_art=False,reason='Progress saved'):
        entries={}
        for member_id,row in self.state['members'].items():
            base=Path(row['workspace']); files={}
            saved={'status':'deferred','saved':False}
            if self.plan['kind']=='software': saved={'status':'not_required','saved':True}
            elif save_art and row['role']=='vfx':
                try:
                    if self.checkpoint_hook: saved=self.checkpoint_hook(self,row)
                    else:
                        ctx=self.context(self.members[member_id],row['phase'])
                        port=ctx.config.get('blender_port')
                        identity=ctx.config.get('_v2_blender_identity')
                        if port and identity: saved={'status':'saved',**studio_blender.save_checkpoint(self.root,identity,port,job_dir=base)}
                        else: saved={'status':'unverified','saved':False,'message':'No verified member Blender instance is recorded.'}
                except Exception as error: saved={'status':'unverified','saved':False,'message':self.safe(str(error))}
            for name in ('game','art','references'):
                for relative,path in project_files(base/name).items(): files[name+'/'+relative]=digest(path)
            entries[member_id]={'status':row['status'],'attempts':row['attempts'],'workspace':str(base),'files':files,'blender_checkpoint':saved}
        metadata={'schema':'ai-studio-team-checkpoint/v2','job_id':self.job.name,'pipeline_id':self.job.name,
                  'state':self.state['status'],'reason':reason,'updated_at':time.time(),'members':entries,'accounting':self.ledger.snapshot()}
        self.dump(self.job/'checkpoint'/'metadata.json',metadata); self.persist()
        return metadata

    def version(self):
        folder=plain(self.job/'checkpoint'/'versions'); folder.mkdir(parents=True,exist_ok=True)
        target=folder/('checkpoint-'+str(time.time_ns())+'.zip')
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            for name in ('game','art','references'):
                for relative,path in project_files(self.job/name).items(): archive.write(path,name+'/'+relative)
            for row in self.state['members'].values():
                base=Path(row['workspace'])
                for name in ('game','art','references'):
                    for relative,path in project_files(base/name).items(): archive.write(path,str(path.relative_to(self.job)))
            for name in ('pipeline.v2.json','.team-state.json','checkpoint/metadata.json'):
                path=self.job/name
                if path.is_file(): archive.write(path,name)
        for expired in sorted(folder.glob('checkpoint-*.zip'),key=lambda item:item.stat().st_mtime,reverse=True)[5:]: plain(expired).unlink()
        return target

    def recover(self,action,member_id=None):
        self.apply_revision()
        if self.state['status']=='CANCELLED': raise RuntimeError('Save a new task before starting a cancelled pipeline.')
        for row in self.state['members'].values():
            if row['status']=='RUNNING': row.update(status='INTERRUPTED',error='Previous owner ended; explicit member retry is required.')
        if action=='retry':
            if member_id not in self.state['members']:
                raise RuntimeError('Explicit retry requires a registered team member.')
            target=self.state['members'][member_id]
            stage=self.state['stages'][target['stage']]
            curator_failed=stage.get('lead_id')==member_id and stage.get('curation_status') in ('FAILED','BLOCKED','INTERRUPTED')
            allowed={'FAILED','BLOCKED','INTERRUPTED','BUDGET_STOP','WAITING_CONNECTION','WAITING_AUTHORIZATION','WAITING_APPROVAL','WAITING_BUDGET'}
            if target['status'] not in allowed and not curator_failed:
                raise RuntimeError('Explicit retry requires one failed, blocked, or waiting member/curator.')
            if curator_failed: target['phase']='curation'
        if action in ('resume','retry'):
            snapshot=self.ledger.snapshot()
            if snapshot.get('unknown_invocations') or snapshot.get('interrupted_invocations'):
                raise RuntimeError('Real paid costs must be reconciled before this pipeline resumes.')
            for marker in ('pause.request','.controlpause'): plain(self.job/marker).unlink(missing_ok=True)
            for row in self.state['members'].values():
                if row['status'] in ('PAUSED','WAITING_CONNECTION','WAITING_BUDGET','WAITING_APPROVAL'):
                    if row['status']=='WAITING_BUDGET':
                        request=next((item for item in load_budget_requests(self.job)['requests'] if item['id']==row.get('budget_request_id')),None)
                        member=self.members[row['id']]
                        expanded=request is not None and self.plan.get('revision',0)>request['plan_revision'] and (
                            Decimal(str(member['budget_usd']))>Decimal(str(request['current_member_cap_usd'])) or
                            Decimal(str(self.plan['roles'][member['role']]['budget_usd']))>Decimal(str(request['current_role_cap_usd'])) or
                            Decimal(str(self.plan['total_budget_usd']))>Decimal(str(request['total_cap_usd'])))
                        if request is None or (request['status']!='APPROVED' and not expanded):
                            continue  # Resume approval alone cannot grant a pending USD request.
                    pending=row.get('reference_request')
                    row['status']='WAITING_REFERENCE' if pending and self.state['references'].get(pending,{}).get('status')!='DONE' else 'READY'
            for request in self.state['references'].values():
                if request.get('task_epoch',0)!=self.plan.get('task_epoch',0) or request['status'] in ('STALE','CANCELLED'): continue
                for run in request['runs'].values():
                    if run['status'] in ('PAUSED','WAITING_CONNECTION','WAITING_BUDGET','WAITING_APPROVAL'): run['status']='READY'
            if action=='retry':
                row=self.state['members'][member_id]; row['status']='READY'
                if row['role']=='research' and row.get('request_id'):
                    self.state['references'][row['request_id']]['runs'][member_id]['status']='READY'
        self.stop.clear()

    def run(self,action='start',member_id=None):
        with self.lease():
            self.recover(action,member_id)
            if self.state['status']=='DONE':
                self.checkpoint(save_art=False,reason='Completed work retained after an explicit budget-only resume; zero new model calls.')
                return self.state
            if self.pause_requested():
                self.state['status']='PAUSED'; self.checkpoint(save_art=True,reason='Pause retained; zero new dispatch.'); return self.state
            policy=self.plan.get('policy',{})
            maximum=policy.get('max_parallel_members',max(1,len(self.members)))
            if isinstance(maximum,bool) or not isinstance(maximum,int) or maximum<1:
                raise RuntimeError('Member concurrency must be a positive local resource limit.')
            grace=float(policy.get('pause_grace_seconds',30))
            if not 0<=grace<=60: raise RuntimeError('Pause grace must be from zero through sixty seconds.')
            futures={}; pausing=None; stopped=False; last_save=time.monotonic()
            self.state['status']='RUNNING'; self.persist()
            with ThreadPoolExecutor(max_workers=maximum) as pool:
                while True:
                    if self.pause_requested() and pausing is None:
                        pausing=time.monotonic(); self.state['status']='PAUSING'; self.checkpoint(reason='Pause requested; no new member work.')
                    if pausing is not None and futures and not stopped and time.monotonic()-pausing>=grace:
                        stopped=True; self.stop.set()
                        for ctx in list(self.contexts.values()):
                            if ctx.member: ctx.module.stop_all(); break
                    if pausing is None:
                        for row in self.state['members'].values():
                            if row['status']=='WAITING_RESERVATION': row['status']='READY'
                        self.advance_roles()
                        for member_id,member in self.members.items():
                            if member['role']=='research': continue
                            row=self.state['members'][member_id]
                            if member['role']=='integration' and not all(self.state['stages'][ROLE_STAGE[dep]]['status']=='DONE' for dep in ('gameplay','vfx')): continue
                            self._submit(pool,futures,member,row['phase'])
                        self.submit_research(pool,futures)
                    if time.monotonic()-last_save>=15 and policy.get('autosave',True):
                        self.checkpoint(reason='Periodic task state and on-disk artifacts saved.'); last_save=time.monotonic()
                    if not futures:
                        if pausing is None:
                            self.advance_roles(); self.acceptance()
                            if any(row['status']=='READY' for row in self.state['members'].values() if row['role']!='research') and not self.pause_requested(): continue
                        break
                    done,_=wait(futures,return_when=FIRST_COMPLETED,timeout=.5)
                    for future in done:
                        member,ctx=futures.pop(future); self._finish(future,member,ctx)
            if self.pause_requested() or pausing is not None:
                for row in self.state['members'].values():
                    if row['status']=='READY': row['status']='PAUSED'
                self.state['status']='PAUSED'
            elif self.state['stages']['qa']['status']=='DONE': self.state['status']='DONE'
            else: self.state['status']='WAITING' if any(row['status'].startswith('WAITING') for row in self.state['members'].values()) else 'NEEDS_ATTENTION'
            self.checkpoint(save_art=self.state['status']!='DONE',reason='Owning member invocations ended; candidate progress retained.')
            self.version(); self.stop.clear(); return self.state
