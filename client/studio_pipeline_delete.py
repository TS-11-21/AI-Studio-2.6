"""Delete one idle isolated pipeline, retaining minimal local accounting audit.

No model/API calls. Tombstones apply to this data root; they are not account-
server entitlements. Native Blender closure uses verified OS process handles,
never forbidden model-side quit code or an unsafe-mode override.
"""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import ctypes,json,os,re,shutil,socket,sqlite3,struct,time

import store_cancel as cancel
from studio_task_cycles import atomic,pipeline_edit_lease
from studio_service import JOB_ID

SCHEMA='ai-studio-pipeline-deletion/v1'
STATUSES={'DELETING','DELETE_FAILED','DELETED'}
class DeleteError(ValueError):pass

def expected_confirmation(job_id):
    if not isinstance(job_id,str) or not JOB_ID.fullmatch(job_id):raise DeleteError('Select one valid pipeline ID.')
    return 'DELETE '+job_id
def _directory(root,job_id):
    expected_confirmation(job_id);root=cancel._plain(root)
    return cancel._plain(root/'.control/deleted-pipelines'/job_id,inside=root)
def reject_deleted_id(root,job_id):
    directory=_directory(root,job_id)
    if directory.exists():raise DeleteError('This pipeline ID has a permanent deletion record. Choose a new ID; its historical costs and attempts cannot be reset.')
def _read(path):return cancel._read(path)
def _audit(root,job_id):
    path=_directory(root,job_id)/'audit.json'
    return _read(path) if path.is_file() else None
def _config(service,job):
    c=dict(service._config())
    for name in ('pipeline.v2.json','pipeline.v3.json'):
        if (job/name).is_file():c.update(_read(job/name).get('config',{}))
    return c
def _costs(service,job,rows,c):
    evidence=cancel._minimum_evidence(job,rows)
    if evidence['cost_reconciliation_required']:raise DeleteError('费用仍有未核清或未结束的调用。请先完成原账本对账，再删除；原目录和费用记录已保留。')
    # Validate the existing ledger's frozen schema/caps; never create a ledger.
    for name in ('.graph-budget.json','.team-budget.json','.adapter-state/budget.json'):
        path=job/name
        if not path.is_file():continue
        if name=='.graph-budget.json':
            from studio_v3_service import budget_caps
            from studio_v3_budget import GraphBudgetLedger
            plan=_read(job/'pipeline.v3.json');roles,members=budget_caps(plan);GraphBudgetLedger(path,plan['total_budget_usd'],roles,members).snapshot()
        elif name=='.team-budget.json':
            from studio_v2_service import budget_caps
            from studio_v2_budget import TeamBudgetLedger
            plan=_read(job/'pipeline.v2.json');roles,members=budget_caps(plan);TeamBudgetLedger(path,plan['total_budget_usd'],roles,members).snapshot()
        else:
            from adapter_budget import BudgetLedger
            BudgetLedger(path,_read(path)['total_cap_usd']).snapshot()
        break
    from studio_invocation_provenance import nonmetered
    p=SimpleNamespace(ROOT=service.root,ensure_plain_path=cancel._plain)
    # Raw SDK logs are discarded only after their actual billing is proven.
    for folder in job.rglob('.pipeline'):
        cancel._plain(folder,inside=job);workspace=folder.parent
        for usage in folder.glob('*.usage.json'):
            tag=usage.name[:-len('.usage.json')]
            if nonmetered(p,c,workspace,tag):continue
            value=_read(usage)
            if value.get('reported_total_cost_usd') is None:raise DeleteError('原调用的实际费用还未确认，不能删除其证明文件。')
            cancel._money(value['reported_total_cost_usd'])
        for log in folder.glob('claude_vfx-*.log'):
            if not re.fullmatch(r'claude_vfx-\d+\.log',log.name) or log.with_suffix('.usage.json').is_file():continue
            if nonmetered(p,c,workspace,log.stem):continue
            finals=[]
            for line in log.read_text('utf-8',errors='replace').splitlines():
                try:value=json.loads(line)
                except ValueError:continue
                if isinstance(value,dict) and value.get('type')=='result':finals.append(value)
            if not finals or finals[-1].get('total_cost_usd') is None:raise DeleteError('历史调用缺少最终费用；先核账再删除，不能将其当作免费。')
            cancel._money(finals[-1]['total_cost_usd'])
    return evidence

class _WindowsProcess:
    def __init__(self,pid):
        self.pid=pid;self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong];self.kernel.OpenProcess.restype=ctypes.c_void_p
        self.kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        self.kernel.QueryFullProcessImageNameW.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_ulong)]
        self.kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
        self.kernel.TerminateProcess.argtypes=[ctypes.c_void_p,ctypes.c_uint]
        self.handle=self.kernel.OpenProcess(0x1000|0x100000|1,False,pid)
        if not self.handle:
            error=ctypes.get_last_error()
            if error==87:self.handle=None;return
            raise DeleteError('无法安全验证或关闭该 Blender 进程。请亲自关闭该流水线的独立实例后再删除。')
    def alive(self):return bool(self.handle) and self.kernel.WaitForSingleObject(self.handle,0)==258
    def executable(self):
        buffer=ctypes.create_unicode_buffer(32768);size=ctypes.c_ulong(len(buffer))
        if not self.kernel.QueryFullProcessImageNameW(self.handle,0,buffer,ctypes.byref(size)):raise DeleteError('Blender 进程的真实程序路径无法核验。')
        return Path(buffer.value).absolute()
    def terminate(self):
        if self.alive() and not self.kernel.TerminateProcess(self.handle,0):raise DeleteError('已核验的独立 Blender 未能关闭；本次不删除文件。')
        if self.handle and self.kernel.WaitForSingleObject(self.handle,5000)!=0:raise DeleteError('独立 Blender 仍未结束；本次不删除文件。')
    def close(self):
        if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None
def _open_process(pid):
    if os.name!='nt':raise DeleteError('This deletion build verifies Blender ownership on Windows only.')
    return _WindowsProcess(pid)
def _tcp_owner(port):
    if os.name!='nt':raise DeleteError('Windows loopback process ownership cannot be checked.')
    dll=ctypes.WinDLL('iphlpapi',use_last_error=True);size=ctypes.c_ulong(0)
    api=dll.GetExtendedTcpTable
    api.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong),ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
    result=api(None,ctypes.byref(size),False,2,5,0)
    if result not in (0,122):raise DeleteError('无法核验 Blender 本机端口的进程归属。')
    buffer=ctypes.create_string_buffer(size.value)
    if api(buffer,ctypes.byref(size),False,2,5,0):raise DeleteError('无法读取 Blender 本机端口归属。')
    count=struct.unpack_from('<I',buffer.raw)[0];owners=[]
    for i in range(count):
        state,addr,p,remoteaddr,remoteport,pid=struct.unpack_from('<6I',buffer.raw,4+i*24)
        if state==2 and socket.ntohs(p&0xffff)==port:
            if socket.inet_ntoa(struct.pack('<I',addr))!='127.0.0.1':raise DeleteError('Blender 端口不是独立 loopback 服务；拒绝删除。')
            owners.append(pid)
    if len(owners)>1:raise DeleteError('Blender 端口归属有冲突；拒绝删除。')
    return owners[0] if owners else None
def _identity(port):
    # Same permitted readonly scene identity probe used by the normal backend.
    import studio_blender
    return studio_blender.identity(port)

def _instances(application,job,c):
    if getattr(application,'generation',1)==3:
        import studio_v3_blender as bridge
    else:import studio_blender as bridge
    runtime=cancel._plain(bridge.runtime_directory(application.root,c))
    states={};selected={}
    for path in runtime.glob('blender-*-state.json') if runtime.exists() else []:
        cancel._plain(path,inside=runtime)
        try:value=_read(path)
        except ValueError:continue  # An unrelated broken cache is not an ownership claim.
        source=value.get('source') if isinstance(value,dict) else None
        if not isinstance(source,str):continue
        candidate=Path(source).absolute()
        if candidate.is_relative_to(job):
            cancel._plain(candidate,inside=job);states[value.get('port')]=(value,path)
    for meta in job.rglob('.studio-runtime.json'):
        cancel._plain(meta,inside=job);value=_read(meta)
        selected[value.get('blender_port')]=(value,meta.parent/'art/source.blend')
    for port,(state,path) in states.items():
        if port in selected:
            meta,source=selected[port]
            if meta.get('blender_pid')!=state.get('pid') or meta.get('job_id')!=state.get('job') or Path(state['source'])!=source:
                raise DeleteError('Blender 实例记录互相冲突；本次不删除。')
        else:selected[port]=({'blender_pid':state.get('pid'),'job_id':state.get('job')},Path(state['source']))
    results=[]
    for port,(meta,source) in selected.items():
        pid=meta.get('blender_pid');alias=meta.get('job_id')
        if isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535 or isinstance(pid,bool) or not isinstance(pid,int) or pid<=0 or not isinstance(alias,str) or not alias:
            raise DeleteError('Blender 实例身份记录不完整；请确认并关闭该独立实例后再删除。')
        source=cancel._plain(source,inside=job)
        process=_open_process(pid)
        try:
            owner=_tcp_owner(port)
            if not process.alive():
                if owner is not None:raise DeleteError('原 Blender 已结束，但端口被别的进程使用；请核验后再删除。')
                process.close();continue
            if port not in states:raise DeleteError('缺少独立 Blender 启动证明，不能代为关闭它或删除此流水线。')
            state,path=states[port]
            if state.get('ready') is not True or state.get('host')!='127.0.0.1' or path.name!='blender-'+str(port)+'-state.json':raise DeleteError('独立 Blender 启动证明无效。')
            if source.name!='source.blend' or source.parent.name!='art':raise DeleteError('Blender 源文件不属于该流水线的素材目录。')
            profile=cancel._plain(runtime/'blender-profiles'/str(port)/'config',inside=runtime)
            if not isinstance(state.get('user_config'),str) or Path(state['user_config']).absolute()!=profile:raise DeleteError('Blender 未使用该版本的隔离配置目录。')
            expected=cancel._plain(Path(c.get('blender_path','C:/steam/steamapps/common/Blender/blender.exe')).absolute())
            if not expected.is_file() or expected.name.casefold()!='blender.exe' or process.executable()!=expected or owner!=pid:
                raise DeleteError('Blender 进程、程序路径或端口归属不一致；不会关闭别人的实例。')
            identity=_identity(port)
            if identity.get('isolated') is not True or identity.get('job')!=alias or not isinstance(identity.get('file'),str) or Path(identity['file']).absolute()!=source:
                raise DeleteError('Blender 当前场景与该任务不一致；请亲自确认该独立实例，原工程不会被关闭。')
            results.append({'process':process,'pid':pid,'port':port,'source':source,'alias':alias,'state_sha256':cancel._hash(path.read_bytes())})
        except Exception:
            process.close()
            for existing in results:existing['process'].close()
            raise
    return results
def _close_instances(instances):
    for item in instances:
        identity=_identity(item['port'])
        if _tcp_owner(item['port'])!=item['pid'] or identity.get('isolated') is not True or identity.get('job')!=item['alias'] or Path(identity.get('file','')).absolute()!=item['source']:
            raise DeleteError('关闭前的 Blender 身份再次核验失败；原目录保留。')
        item['process'].terminate()
def _mark(service,job_id,status):
    path=cancel._plain(service.database_path,inside=service.root)
    if path.is_file():
        with closing(sqlite3.connect(path,timeout=10)) as database,database:
            database.execute('UPDATE stages SET status=?,detail=?,updated=? WHERE job=?',(status,'Pipeline deletion: cumulative attempts retained.',time.time(),job_id))
def _clear_cache(service,job_id):
    with service._guard:
        service._runtime.pop(job_id,None);service._threads.pop(job_id,None)
        service._events=[event for event in service._events if event.get('job_id')!=job_id]
        service._saved_events.pop(job_id,None)
        service._log_changes=[event for event in service._log_changes if event.get('job_id')!=job_id]
        service._log_seen={key:value for key,value in service._log_seen.items() if not Path(key).is_relative_to(Path('jobs')/job_id)}

def _execution_history(service,job):
    """Retain host-recorded completion/counters, never prompts or tool content."""
    stages=[]
    if service.database_path.is_file():
        with closing(sqlite3.connect(service.database_path,timeout=10)) as database:
            stages=[{'stage_id_hash':cancel._hash(name),'status':status,'attempts':cancel._count(attempts)}
                for name,status,attempts in database.execute('SELECT name,status,attempts FROM stages WHERE job=?',(job.name,))]
    states=[]
    for name in ('.graph-state.json','.team-state.json'):
        if not (job/name).is_file():continue
        value=_read(job/name)
        states.append({'state_schema':value.get('schema'),'status':value.get('status'),'plan_revision':value.get('plan_revision'),
            'qa_status':value.get('qa',value.get('stages',{}).get('qa',{})).get('status'),
            'members':[{'member_id_hash':cancel._hash(identifier),'status':member.get('status'),'attempts':cancel._count(member.get('attempts',0)),
                'runs':[{'run_id_hash':cancel._hash(rid),'status':run.get('status'),'attempts':cancel._count(run.get('attempts',0))}
                        for rid,run in member.get('runs',{}).items()]} for identifier,member in value.get('members',{}).items()]})
    report=job/'delivery/qa_report.json';qa=None
    if report.is_file():
        value=_read(report)
        qa={'report_sha256':cancel._hash(report.read_bytes()),'passed':value.get('passed') is True,
            'automated_checks_passed':value.get('automated_checks')=='pass','status':value.get('status'),'attempt':value.get('attempt')}
    completed=any(stage['stage_id_hash']==cancel._hash('qa') and stage['status']=='DONE' for stage in stages) or any(state['qa_status']=='DONE' for state in states)
    return {'stages':stages,'engine_states':states,'saved_qa':qa,'host_recorded_qa_done_before_delete':completed,
        'trial_usage_tables_modified':False,'note':'Historical host evidence retained; deletion is not a credit/refund and does not authorize recalculating trial usage as zero.'}

def delete_pipeline(application,job_id,confirmation):
    if confirmation!=expected_confirmation(job_id):raise DeleteError('Explicit confirmation must exactly match DELETE '+job_id+'.')
    root=cancel._root(application);service=application.service;directory=_directory(root,job_id);audit_path=directory/'audit.json'
    with service._lock_for(job_id):
        previous=_audit(root,job_id)
        if previous and previous.get('status')=='DELETED':return {'job_id':job_id,'status':'DELETED','already_deleted':True,'pipeline_removed':True}
        job=cancel._plain(root/'jobs'/job_id,inside=root/'jobs')
        if previous and previous.get('status') in ('DELETING','DELETE_FAILED') and not job.exists():
            if previous.get('schema')!=SCHEMA or previous.get('job_id')!=job_id or not isinstance(previous.get('evidence'),dict):raise DeleteError('Original deletion accounting cannot be verified; ID remains blocked.')
            previous.update(status='DELETED',finished_at=time.time());atomic(audit_path,previous);_mark(service,job_id,'DELETED');_clear_cache(service,job_id)
            return {'job_id':job_id,'status':'DELETED','finalized_existing_deletion':True,'pipeline_removed':True,'id_reusable':False,'accounting_retained':True}
        if not job.is_dir():raise DeleteError('The initialized isolated pipeline is missing; its deletion audit cannot be invented.')
        instances=[]
        try:
            with pipeline_edit_lease(job):
                rows=cancel._idle(application,job_id,job)
                transport=getattr(application,'transport',None) or getattr(service,'transport',None)
                preview=getattr(transport,'preview_processes',{}).get(job_id)
                if preview is not None and preview.poll() is None:raise DeleteError('该流水线的预览程序仍在运行。请先关闭它再删除；不会关闭其他软件。')
                for path in job.rglob('*'):cancel._plain(path,inside=job)
                if not (job/'.pipeline_job').is_file() or (job/'.pipeline_job').read_text('ascii').strip()!='isolated-demo-v1':raise DeleteError('Only initialized isolated pipelines may be deleted.')
                c=_config(service,job);current_evidence=_costs(service,job,rows,c)
                evidence=previous['evidence'] if previous and previous.get('status') in ('DELETING','DELETE_FAILED') and isinstance(previous.get('evidence'),dict) else current_evidence
                instances=_instances(application,job,c)
                history=previous.get('execution_history') if previous and previous.get('execution_history') else _execution_history(service,job)
                audit={'schema':SCHEMA,'job_id':job_id,'status':'DELETING','confirmed_at':time.time(),'evidence':evidence,'execution_history':history,
                    'trial_counters_changed':False,'model_costs_reversed':False,'id_reusable':False,'scope':'This data root only.',
                    'blender_instances':[{'pid_hash':cancel._hash(i['pid']),'alias_hash':cancel._hash(i['alias']),'source_path_hash':cancel._hash(i['source']),'state_sha256':i['state_sha256']} for i in instances]}
                atomic(audit_path,audit)  # Durable tombstone/accounting before process or file mutation.
                _close_instances(instances)
                _mark(service,job_id,'DELETING')
                for child in list(job.iterdir()):
                    if child.name in ('.team-owner.lock','.pipeline_job'):continue
                    cancel._plain(child,inside=job)
                    if child.is_dir():shutil.rmtree(child)
                    else:child.unlink()
            # On Windows the owner lease's open file must close before root removal.
            for name in ('.team-owner.lock','.pipeline_job'):(job/name).unlink(missing_ok=True)
            job.rmdir();_mark(service,job_id,'DELETED');_clear_cache(service,job_id)
            audit.update(status='DELETED',finished_at=time.time());atomic(audit_path,audit)
            return {'job_id':job_id,'status':'DELETED','pipeline_removed':True,'id_reusable':False,'accounting_retained':True,'trial_counters_changed':False}
        except Exception:
            if audit_path.is_file():
                audit=_read(audit_path);audit.update(status='DELETE_FAILED',error='Deletion incomplete; original ID remains blocked. Review retained accounting and remaining files before retry.')
                atomic(audit_path,audit);_mark(service,job_id,'DELETE_FAILED')
            raise
        finally:
            for item in instances:item['process'].close()

def decorate_deleted_state(application,state):
    root=cancel._root(application)
    removed=set()
    for row in state.get('jobs',[]):
        record=_audit(root,row['job_id'])
        if record is None and _directory(root,row['job_id']).exists():record={'status':'DELETE_FAILED','error':'Deletion accounting did not persist; original files are kept. Resolve filesystem permission and retry deletion.'}
        if record:
            status=record.get('status','DELETE_FAILED')
            if status=='DELETED':removed.add(row['job_id']);continue
            row.update(status=status,editable=False,edit_mode='locked',can_start=False,pipeline_deletion_pending=True,
                deletion_error=record.get('error'),id_reusable=False)
    state['jobs']=[row for row in state.get('jobs',[]) if row['job_id'] not in removed]
    state['budgets']={key:value for key,value in state.get('budgets',{}).items() if key not in removed}
    for key in ('latest_events','log_changes'):
        state[key]=[row for row in state.get(key,[]) if row.get('job_id') not in removed]
    state['running_jobs']=[row['job_id'] for row in state['jobs'] if row.get('running')]
    state['running_job']=state['running_jobs'][0] if state['running_jobs'] else None
    state.setdefault('app',{}).setdefault('capabilities',{})['pipeline_deletion']=True
    return state

def deletion_history(application):
    root=cancel._root(application);directory=cancel._plain(root/'.control/deleted-pipelines',inside=root)
    return [_read(path) for path in sorted(directory.glob('*/audit.json'))] if directory.exists() else []
