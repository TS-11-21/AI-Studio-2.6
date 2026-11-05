"""Explicit cancellation of one stopped isolated job in the preparation client.

This module never pauses/kills an active process itself, invokes a model, calls
a payment/credit service, or reads private credentials. Cooperative job locks
and the runner lease protect against normal resume/edit races. Filesystem
mutation by a hostile process with the same OS user's rights is not prevented.
"""
from __future__ import annotations
from contextlib import closing, nullcontext
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time

from studio_task_cycles import pipeline_edit_lease, atomic
from studio_service import JOB_ID

PROTECTED_ROOT_NAMES = {'ai-studio','ai-studio-v2','ai-studio-v2.5','ai-studio-v2.6','ai-studio-v3'}
BUSY = {'RUNNING','PAUSING','CHECKING','CURATING'}
PRESERVED = {'.pipeline_job','.team-owner.lock','cancelled.json','task.json','pipeline.v2.json','pipeline.v3.json','.workers.json','mcp.v2.json','mcp.v3.json','.team-budget.json','.graph-budget.json','.task-cycles.json','.plan-revisions.json','.graph-revisions.json','.budget-requests.json','.team-state.json','.graph-state.json','.studio-runner.json'}
SCHEMA = 'ai-studio-cancellation-audit/v1'


class CancelError(ValueError):
    pass


def expected_confirmation(job_id):
    if not isinstance(job_id,str) or not JOB_ID.fullmatch(job_id):
        raise CancelError('Select one valid isolated task ID.')
    return 'CANCEL '+job_id


def _plain(path, *, inside=None):
    path=Path(path).absolute()
    if path.resolve()!=path or inside is not None and not path.is_relative_to(Path(inside).absolute()):
        raise CancelError('Cancellation refuses redirected or out-of-scope paths.')
    for part in (path,*path.parents):
        try: info=part.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
            raise CancelError('Cancellation refuses symlinks, junctions and other reparse points.')
    return path


def _root(application):
    service=application.service
    root=_plain(service.root)
    approved_personal=(root.name.casefold()=='ai-studio-v3' and getattr(application,'generation',None)==3 and getattr(application,'personal_runtime_mode',False) is True and root==Path(__file__).resolve().parent)
    if root.name.casefold() in PROTECTED_ROOT_NAMES and not approved_personal:
        raise CancelError('Earlier studio versions are preserved; cancellation is only enabled in the independent preparation client.')
    if Path(application.root).absolute()!=root:
        raise CancelError('Application and service task roots disagree.')
    return root


def _audit_path(application,job_id):
    expected_confirmation(job_id)
    return _plain(_root(application)/'.control'/'cancelled-jobs'/job_id/'audit.json',inside=_root(application))


def _read(path):
    _plain(path)
    try: return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (ValueError,OSError): raise CancelError('Task accounting/status cannot be read; cleanup is stopped rather than losing evidence.') from None


def reject_cancelled(application,job_id,*,allow_edit=False):
    marker=_plain(_root(application)/'jobs'/job_id/'cancelled.json',inside=_root(application))
    if not marker.exists(): return
    status=_read(marker).get('status')
    if status in ('CANCELLING','CANCEL_FAILED'):
        raise CancelError('Task cleanup is incomplete; repair it before editing or restarting.')
    if status=='CANCELLED' and not allow_edit:
        raise CancelError('This task was cancelled. Keep the pipeline and save a new task before starting.')


def _pid_alive(pid):
    if isinstance(pid,bool) or not isinstance(pid,int) or pid<=0: return False
    if os.name=='nt':
        import ctypes
        kernel=ctypes.windll.kernel32
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle: return ctypes.get_last_error()==5
        try:
            code=ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally: kernel.CloseHandle(handle)
    try: os.kill(pid,0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


def _idle(application,job_id,job):
    service=application.service
    with getattr(service,'_guard',nullcontext()):
        thread=getattr(service,'_threads',{}).get(job_id)
        runtime=dict(getattr(service,'_runtime',{}).get(job_id,{}))
    if thread and thread.is_alive(): raise CancelError('Pause this task first and wait for its worker thread to exit.')
    transport=getattr(application,'transport',None) or getattr(service,'transport',None)
    getter=getattr(transport,'get_job_runtime',None)
    if callable(getter): runtime.update(getter(job_id) or {})
    if runtime.get('running') or str(runtime.get('status','')).upper() in BUSY or _pid_alive(runtime.get('pid')):
        raise CancelError('Pause this task first and confirm its owning process has ended.')
    for name in ('.studio-runner.json','.graph-state.json','.team-state.json'):
        path=job/name
        if not path.exists(): continue
        data=_read(path)
        if not isinstance(data,dict): raise CancelError('Task state has an invalid format.')
        if _pid_alive(data.get('pid')) or str(data.get('status','')).upper() in BUSY:
            raise CancelError('Task is running or still pausing; wait for the saved pause checkpoint.')
        for member in data.get('members',{}).values():
            if str(member.get('status','')).upper() in BUSY or any(str(run.get('status','')).upper() in BUSY for run in member.get('runs',{}).values()):
                raise CancelError('A member is still active; pause and confirm termination before cleanup.')
    checkpoint=job/'checkpoint'/'metadata.json'
    if checkpoint.exists() and str(_read(checkpoint).get('state','')).upper() in BUSY:
        raise CancelError('The pause checkpoint is not complete yet.')
    rows=service._stage_rows(job_id)
    if any(str(row.get('status','')).upper() in BUSY for row in rows):
        raise CancelError('A legacy stage is still active; finish pausing before cleanup.')
    return rows


def _hash(value): return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def _money(value,*,optional=False):
    if value is None and optional: return None
    if isinstance(value,bool): raise CancelError('Invalid recorded cost; cleanup stopped.')
    try: number=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError): raise CancelError('Invalid recorded cost; cleanup stopped.') from None
    if not number.is_finite() or number<0: raise CancelError('Invalid recorded cost; cleanup stopped.')
    return str(number)


def _count(value):
    if isinstance(value,bool) or not isinstance(value,int) or value<0: raise CancelError('Invalid attempt count; cleanup stopped.')
    return value


def _minimum_evidence(job,rows):
    evidence={'legacy_attempts':[{'stage_id_hash':_hash(row['name']),'attempts':_count(row.get('attempts',0))} for row in rows],
        'member_attempts':[],'accounting_records':[],'usage_evidence':[],'accounting_known':False,'cost_reconciliation_required':False}
    for name in ('.graph-state.json','.team-state.json'):
        path=job/name
        if not path.exists(): continue
        data=_read(path)
        for identifier,member in data.get('members',{}).items():
            result={'member_id_hash':_hash(identifier),'attempts':_count(member.get('attempts',0)),
                'phase_attempts':{_hash(key):_count(value) for key,value in member.get('phase_attempts',{}).items()},'runs':[]}
            for run_id,run in member.get('runs',{}).items():
                result['runs'].append({'run_id_hash':_hash(run_id),'attempts':_count(run.get('attempts',0)),
                    'phase_attempts':{_hash(key):_count(value) for key,value in run.get('phase_attempts',{}).items()}})
            evidence['member_attempts'].append(result)
    # One authoritative ledger, not a sum of its duplicated SDK evidence.
    for name in ('.graph-budget.json','.team-budget.json','.adapter-state/budget.json'):
        path=job/name
        if not path.exists(): continue
        data=_read(path)
        entries=data.get('invocations')
        if not isinstance(entries,dict): raise CancelError('Ledger evidence is invalid; reconcile it before cleanup.')
        for identifier,entry in entries.items():
            if not isinstance(entry,dict) or entry.get('state') not in ('pending','unknown','reconciled'):
                raise CancelError('Ledger state is invalid; cleanup stopped.')
            known=entry['state']=='reconciled'
            cost=_money(entry.get('cost_usd'),optional=not known)
            if not known and cost is not None: raise CancelError('Unproven cost record is invalid; cleanup stopped.')
            evidence['accounting_records'].append({'invocation_id_hash':_hash(identifier),
                'provider_id_hash':_hash(entry.get('provider','')),'station_id_hash':_hash(entry.get('role','')),
                'member_id_hash':_hash(entry.get('member','')),'state':entry['state'],'cost_usd':cost,
                'reserved_cap_usd':_money(entry.get('reserved_cap_usd',0)),
                'native_usage_path_hash':_hash(entry.get('metadata',{}).get('native_usage_path',''))})
            evidence['cost_reconciliation_required'] |= not known
        evidence['accounting_known']=True; evidence['ledger_file_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        break
    for path in job.rglob('*.usage.json'):
        _plain(path,inside=job); data=_read(path)
        selected={key:_money(data[key],optional=True) for key in ('reported_total_cost_usd','total_cost_usd','cli_budget_usd') if key in data}
        if selected: evidence['usage_evidence'].append({'usage_path_hash':_hash(path.relative_to(job).as_posix()),'amounts':selected,
            'evidence_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    evidence['known_recorded_spend_usd']=str(sum((Decimal(item['cost_usd']) for item in evidence['accounting_records'] if item['cost_usd'] is not None),Decimal(0)))
    return evidence


def _mark_database(service,job_id):
    database=_plain(service.database_path,inside=service.root)
    if not database.exists(): return
    with closing(sqlite3.connect(database,timeout=10)) as db,db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'stages' in tables:
            columns={row[1] for row in db.execute('PRAGMA table_info(stages)')}
            if {'job','status','updated'}<=columns:
                db.execute('UPDATE stages SET status=?,updated=? WHERE job=?',('CANCELLED',time.time(),job_id))


def _cancel_state(job):
    for name in ('.team-state.json','.graph-state.json'):
        path=job/name
        if not path.is_file(): continue
        data=_read(path); data['status']='CANCELLED'
        for row in data.get('members',{}).values():
            row['status']='CANCELLED'
            for key in ('summary','result','production_result','files','reference_query','last_qa_error'):row.pop(key,None)
            for run in row.get('runs',{}).values():
                run['status']='CANCELLED'
                for key in ('summary','result','files','query','dispatch_prompt','dispatch_inputs','reference_query'):run.pop(key,None)
        for row in data.get('stations',data.get('stages',{})).values():
            row['status']='CANCELLED'
            for run in row.get('runs',{}).values():
                run['status']='CANCELLED'
                for key in ('summary','result','files','query','dispatch_prompt','dispatch_inputs','reference_query'):run.pop(key,None)
        for group in list(data.get('calls',{}).values())+list(data.get('references',{}).values()):
            group['status']='CANCELLED'
            for key in ('query','result','files','summary','dispatch_prompt','dispatch_inputs'):group.pop(key,None)
            for run in group.get('runs',{}).values():run['status']='CANCELLED'
        if isinstance(data.get('qa'),dict):data['qa'].update(status='CANCELLED',expired=True)
        atomic(path,data)
    path=job/'.studio-runner.json'
    if path.is_file():
        data=_read(path);data.update(status='cancelled',ended_at=time.time());atomic(path,data)

def _prune_content(path,job):
    _plain(path,inside=job)
    if path.is_dir():
        if path.name=='.pipeline':
            for item in path.iterdir():
                _plain(item,inside=job)
                if item.is_file() and item.name.endswith('.usage.json'):continue
                if item.is_file() and __import__('re').fullmatch(r'claude_vfx-[0-9]+\.log',item.name):
                    # Keep only actual final billing metadata, not task content.
                    known=[]
                    for line in item.read_text('utf-8',errors='replace').splitlines():
                        try:value=json.loads(line)
                        except ValueError:continue
                        if isinstance(value,dict) and value.get('type')=='result' and value.get('total_cost_usd') is not None:
                            known.append({'type':'result','total_cost_usd':value['total_cost_usd']})
                    item.write_text(json.dumps(known[-1])+'\n' if known else 'Task content cancelled; historical cost remains unproven.\n','utf-8')
                    continue
                if item.is_dir():shutil.rmtree(item)
                else:item.unlink()
            return
        for child in list(path.iterdir()):_prune_content(child,job)
        # Keep ancestor containers for preserved .pipeline billing metadata.
        if not any(path.iterdir()):path.rmdir()
    else:path.unlink()

def cancel_job(application,job_id,confirmation):
    expected=expected_confirmation(job_id)
    if confirmation!=expected: raise CancelError('Explicit confirmation must exactly match '+expected+'.')
    root=_root(application);service=application.service;audit_path=_audit_path(application,job_id)
    with service._lock_for(job_id):
        job=_plain(service._job(job_id),inside=root/'jobs')
        if job!=root/'jobs'/job_id:raise CancelError('Only the selected isolated pipeline task may be cleaned.')
        with pipeline_edit_lease(job):
            _idle(application,job_id,job)
            for path in job.rglob('*'):_plain(path,inside=job)
            if not (job/'task.json').is_file():raise CancelError('Legacy deleted task configuration cannot be reconstructed automatically.')
            task=_read(job/'task.json');epoch=task.get('task_epoch',0)
            marker=_read(job/'cancelled.json') if (job/'cancelled.json').is_file() else {}
            if marker.get('status')=='CANCELLED':return {'job_id':job_id,'status':'CANCELLED','already_cancelled':True,'pipeline_retained':True}
            evidence=_minimum_evidence(job,service._stage_rows(job_id))
            old=_read(audit_path) if audit_path.exists() else None
            if old and old.get('task_epoch')!=epoch:
                atomic(audit_path.parent/'history'/('task-'+str(old.get('task_epoch',0))+'.json'),old)
            audit={'schema':SCHEMA,'job_id':job_id,'task_epoch':epoch,'confirmed_at':time.time(),'evidence':evidence,
                   'pipeline_retained':True,'trial_credits_changed':False,'model_costs_reversed':False,'removed_entries':[],
                   'status':'CANCELLING'}
            atomic(audit_path,audit);atomic(job/'cancelled.json',{'status':'CANCELLING','job_id':job_id,'task_epoch':epoch})
            try:
                _mark_database(service,job_id);_cancel_state(job)
                for path in sorted(job.iterdir(),key=lambda value:value.name):
                    if path.name in PRESERVED:continue
                    if path.name=='checkpoint':
                        for child in list(path.iterdir()):
                            if child.name=='qa-repairs.json':
                                repairs=_read(child);repairs['history']=[];atomic(child,repairs)
                            else:_prune_content(child,job)
                        continue
                    if path.name=='.adapter-state':
                        for child in list(path.iterdir()):
                            if child.name!='budget.json':_prune_content(child,job)
                        continue
                    _prune_content(path,job);audit['removed_entries'].append(path.name)
                for name in ('game','art','references','delivery'):(job/name).mkdir(exist_ok=True)
                atomic(job/'checkpoint/metadata.json',{'state':'CANCELLED','job_id':job_id,'task_epoch':epoch,
                    'accounting':{'cost_reconciliation_required':evidence['cost_reconciliation_required']},
                    'reason':'Task artifacts removed; pipeline/configuration and cumulative accounting retained.'})
                audit.update(status='CANCELLED',finished_at=time.time());atomic(audit_path,audit)
                atomic(job/'cancelled.json',{'status':'CANCELLED','job_id':job_id,'task_epoch':epoch,'cancelled_at':time.time()})
            except Exception:
                audit.update(status='CANCEL_FAILED',error='Task cleanup incomplete; restart is blocked.');atomic(audit_path,audit)
                atomic(job/'cancelled.json',{'status':'CANCEL_FAILED','job_id':job_id,'task_epoch':epoch});raise
            with service._guard:service._runtime[job_id]={'status':'CANCELLED','task_epoch':epoch}
            return {'job_id':job_id,'status':'CANCELLED','pipeline_retained':True,'editable':True,'edit_mode':'cancelled','can_start':False,
                    'task_epoch':epoch,'trial_credits_changed':False,'model_costs_reversed':False,
                    'cost_reconciliation_required':evidence['cost_reconciliation_required']}

def decorate_state(application,state):
    root=_root(application)
    for row in state.get('jobs',[]):
        identifier=row.get('job_id')
        if not isinstance(identifier,str) or not JOB_ID.fullmatch(identifier):continue
        marker=_root(application)/'jobs'/identifier/'cancelled.json'
        if not marker.is_file():continue
        data=_read(marker);status=data.get('status')
        if status not in ('CANCELLED','CANCELLING','CANCEL_FAILED'):continue
        good=status=='CANCELLED'
        row.update(status='CANCELLED' if good else 'CANCEL_FAILED',running=False,cancelled=True,task_cancelled=True,
                   pipeline_retained=True,editable=good,edit_mode='cancelled' if good else 'locked',can_start=False,
                   task_epoch=data.get('task_epoch',0))
    state['running_jobs']=[row['job_id'] for row in state.get('jobs',[]) if row.get('running')]
    state['running_job']=state['running_jobs'][0] if state['running_jobs'] else None
    return state
