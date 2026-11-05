"""Host-owned task replacement metadata; pipeline costs/counters never reset."""
from contextlib import contextmanager,closing
from decimal import Decimal
from pathlib import Path
import json,os,sqlite3,time,stat,uuid

SCHEMA='ai-studio-task-cycles/v1'
def load(path,default=None):
    return json.loads(Path(path).read_text('utf-8-sig')) if Path(path).is_file() else default
def _plain(path):
    path=Path(path).absolute()
    if path.resolve()!=path:raise ValueError('Redirected task state is forbidden.')
    for part in (path,*path.parents):
        try:info=part.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
            raise ValueError('Redirected task state is forbidden.')
    return path
def atomic(path,value):
    path=_plain(path);path.parent.mkdir(parents=True,exist_ok=True);_plain(path.parent)
    temp=_plain(path.with_name(path.name+'.'+uuid.uuid4().hex+'.task-cycle.tmp'))
    try:
        with temp.open('x',encoding='utf-8') as handle:
            handle.write(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False));handle.flush();os.fsync(handle.fileno())
        for attempt in range(20):
            try:os.replace(temp,path);break
            except OSError as error:
                if os.name!='nt' or getattr(error,'winerror',None) not in (5,32,33) or attempt==19:raise
                time.sleep(.025)
    finally:temp.unlink(missing_ok=True)
@contextmanager
def pipeline_edit_lease(job):
    job=Path(job).absolute();path=job/'.team-owner.lock'
    if job.resolve()!=job or path.resolve()!=path.absolute():raise RuntimeError('Redirected pipeline lock is forbidden.')
    with path.open('a+b') as handle:
        handle.seek(0)
        if os.fstat(handle.fileno()).st_size==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt;msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl;fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise RuntimeError('This pipeline still has an owner; wait for pause completion.') from None
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
def cancelled(job):return load(Path(job)/'cancelled.json',{}).get('status') in ('CANCELLED','CANCELLING','CANCEL_FAILED')
def ensure_task_startable(job):
    if cancelled(job):raise ValueError('Save a new task in this retained pipeline before starting. Unfinished cancellation must be repaired first.')
def task_epoch(job):return load(Path(job)/'task.json',{}).get('task_epoch',0)
def legacy_baselines(job):return load(Path(job)/'.task-cycles.json',{}).get('legacy_attempt_baseline',{})
def finish_new_task(service,job,*,new_epoch):
    """Called only after a validated host plan/task commit, under its lease."""
    job=Path(job);marker=load(job/'cancelled.json',{})
    if marker.get('status')!='CANCELLED':raise ValueError('Only a fully cancelled task may be replaced.')
    previous=marker.get('task_epoch',0)
    if new_epoch!=previous+1:raise ValueError('Task epoch must advance exactly once.')
    cycles=load(job/'.task-cycles.json',{'schema':SCHEMA,'history':[]})
    cycles['qa_repair_baseline']=load(job/'checkpoint/qa-repairs.json',{}).get('repairs_used',0)
    cycles.update(current_epoch=new_epoch,legacy_attempt_baseline={row['name']:row.get('attempts',0) for row in service._stage_rows(job.name)})
    cycles['history'].append({'task_epoch':previous,'cancelled_at':marker.get('cancelled_at'),'replaced_at':time.time()})
    atomic(job/'.task-cycles.json',cycles)
    with closing(sqlite3.connect(service.database_path,timeout=10)) as database,database:
        database.execute('UPDATE stages SET status=CASE WHEN name IN ("gpt_base","claude_vfx") THEN "READY" ELSE "DORMANT" END,detail=?,updated=? WHERE job=?',
                         ('New task saved; historical attempts and costs retained.',time.time(),job.name))
    for name in ('pause.request','.controlpause'): (job/name).unlink(missing_ok=True)
    for name in ('game','art','references','delivery'):(job/name).mkdir(exist_ok=True)
    (job/'game/AGENTS.md').write_text('Only this isolated game directory may be edited. Preserve independent acceptance tests.\n','utf-8')
    (job/'art/CLAUDE.md').write_text('Only this isolated art directory may be edited. Never inspect credentials or original projects.\n','utf-8')
    template=Path(service.root)/'templates/project.godot'
    if template.is_file() and not (job/'game/project.godot').exists():
        (job/'game/project.godot').write_bytes(template.read_bytes())
    atomic(job/'cancelled.json',{'status':'TASK_READY','job_id':job.name,'task_epoch':new_epoch,'previous_task_epoch':previous,'at':time.time()})
    atomic(job/'.studio-runner.json',{'job_id':job.name,'status':'ready','task_epoch':new_epoch,'at':time.time()})
    meta=load(job/'checkpoint/metadata.json',{})
    meta.update(state='READY',task_epoch=new_epoch,reason='New task saved. Existing ledger/cumulative attempts retained; no model dispatched.')
    atomic(job/'checkpoint/metadata.json',meta)
    with service._guard:service._runtime[job.name]={'status':'READY','task_epoch':new_epoch}

def pending_task_replacement(job,plan,state):
    """Project only a fully committed host task replacement before engine apply."""
    job=Path(job);marker=load(job/'cancelled.json',{});epoch=plan.get('task_epoch',0)
    applied=state.get('applied_task_epoch',state.get('task_epoch',0))
    if marker.get('status')!='TASK_READY' or marker.get('task_epoch')!=epoch or not isinstance(epoch,int) or isinstance(epoch,bool) or epoch<=applied:return False
    if load(job/'.task-cycles.json',{}).get('current_epoch')!=epoch:return False
    name='.graph-revisions.json' if plan.get('schema')=='ai-studio-pipeline/v3' else '.plan-revisions.json'
    return any(entry.get('task_replaced') is True and entry.get('task_epoch')==epoch and entry.get('previous_task_epoch')==epoch-1
               and 'task_epoch' in entry.get('changed_fields',[]) and entry.get('revision',-1)<=plan.get('revision',0)
               for entry in load(job/name,{}).get('entries',[]))
