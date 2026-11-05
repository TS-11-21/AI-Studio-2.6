"""Local V2.5 edit journals and budget requests. No AI or network calls."""
from __future__ import annotations
from contextlib import contextmanager
from decimal import Decimal
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from studio_v2_budget import amount

REQUEST_SCHEMA = 'ai-studio-budget-requests/v2.5'
REVISION_SCHEMA = 'ai-studio-plan-revisions/v2.5'
JOURNAL_SCHEMA = 'ai-studio-plan-update/v2.5'
TARGETS = frozenset({'pipeline.v2.json', 'task.json', '.workers.json', 'mcp.v2.json',
                     '.team-budget.json', '.plan-revisions.json', '.budget-requests.json'})
STATUSES = frozenset({'PENDING', 'APPROVED', 'DECLINED', 'SUPERSEDED'})


def plain(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise RuntimeError('Redirected plan/accounting paths are not supported.')
    return path


def atomic(path, data):
    path = plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='\n', dir=path.parent,
                                         prefix='.'+path.name+'.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read(path, default=None):
    path = plain(path)
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else copy.deepcopy(default)


def _fingerprint(path):
    path = plain(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _serialized_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def pipeline_edit_lease(job):
    """Same OS lease as the runner: never edit beneath an active owner."""
    job=plain(job); path=plain(job/'.team-owner.lock')
    if not job.is_dir():
        raise RuntimeError('The pipeline directory no longer exists; no edit is allowed.')
    try:
        handle=path.open('a+b')
    except OSError:
        raise RuntimeError('This pipeline is unavailable or has an active owner.') from None
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
            raise RuntimeError('This pipeline has an active owner; pause must finish before editing.') from None
        try:
            yield
        finally:
            if os.name=='nt':
                handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def _recover_locked(job):
    job = plain(job)
    path = job/'.plan-update.journal.json'
    if not path.exists():
        return False
    journal = read(path)
    if (not isinstance(journal, dict) or set(journal) != {'schema', 'pipeline_id', 'targets'} or
            journal['schema'] != JOURNAL_SCHEMA or journal['pipeline_id'] != job.name or
            not isinstance(journal['targets'], dict) or not journal['targets'] or set(journal['targets'])-TARGETS):
        raise RuntimeError('A plan update journal is invalid; no accounting is reset.')
    # Validate every target before writing any of them; a foreign modification
    # cannot be silently overwritten during crash recovery.
    for name, item in journal['targets'].items():
        if not isinstance(item, dict) or set(item) != {'before_sha256', 'after_sha256', 'value'}:
            raise RuntimeError('An incomplete plan update journal requires inspection.')
        if item['after_sha256'] != _serialized_hash(item['value']):
            raise RuntimeError('Plan update journal integrity check failed.')
        if _fingerprint(job/name) not in (item['before_sha256'], item['after_sha256']):
            raise RuntimeError('A plan/accounting file changed outside its pending edit; recovery stopped.')
    for name, item in journal['targets'].items():
        if _fingerprint(job/name) != item['after_sha256']:
            atomic(job/name, item['value'])
    path.unlink()
    return True


def recover_plan_update(job, *, lease_held=False):
    """Roll a prepared edit forward before constructing a strict ledger."""
    if lease_held:
        return _recover_locked(job)
    if not plain(plain(job)/'.plan-update.journal.json').exists():
        return False
    with pipeline_edit_lease(job):
        return _recover_locked(job)


def commit_plan_update(job, targets, *, lease_held=False):
    """Commit related files with a durable, idempotent roll-forward journal."""
    if not lease_held:
        with pipeline_edit_lease(job):
            return commit_plan_update(job, targets, lease_held=True)
    job = plain(job)
    _recover_locked(job)
    if not isinstance(targets, dict) or not targets or set(targets)-TARGETS:
        raise ValueError('Only this pipeline plan/accounting files can participate in an edit.')
    journal = {'schema': JOURNAL_SCHEMA, 'pipeline_id': job.name, 'targets': {}}
    for name, value in targets.items():
        journal['targets'][name] = {'before_sha256': _fingerprint(job/name),
            'after_sha256': _serialized_hash(value), 'value': copy.deepcopy(value)}
    atomic(job/'.plan-update.journal.json', journal)
    _recover_locked(job)


def load_budget_requests(job):
    job = plain(job)
    data = read(job/'.budget-requests.json', {'schema': REQUEST_SCHEMA, 'pipeline_id': job.name, 'requests': []})
    if (not isinstance(data, dict) or set(data) != {'schema', 'pipeline_id', 'requests'} or
            data['schema'] != REQUEST_SCHEMA or data['pipeline_id'] != job.name or not isinstance(data['requests'], list)):
        raise RuntimeError('Budget requests could not be validated.')
    ids = set()
    fields = {'id','member_id','role','status','plan_revision','reason','created_at','current_member_cap_usd',
              'current_role_cap_usd','total_cap_usd','spent_member_usd','suggested_increase_usd'}
    for item in data['requests']:
        if not isinstance(item, dict) or not fields <= set(item) or item['status'] not in STATUSES:
            raise RuntimeError('Malformed budget request.')
        if not isinstance(item['id'], str) or item['id'] in ids:
            raise RuntimeError('Budget request IDs must be unique.')
        ids.add(item['id'])
        if isinstance(item['plan_revision'], bool) or not isinstance(item['plan_revision'], int) or item['plan_revision'] < 0:
            raise RuntimeError('Budget request revision is invalid.')
        for name in ('current_member_cap_usd','current_role_cap_usd','total_cap_usd','spent_member_usd','suggested_increase_usd'):
            amount(item[name])
    return data


def record_budget_request(job, plan, member_id, reason, *, snapshot=None,
                          suggested_increase_usd=None, lease_held=False):
    """Deduplicate a real metered exhaustion request; never grant or resume it."""
    if not lease_held:
        with pipeline_edit_lease(job):
            return record_budget_request(job, plan, member_id, reason, snapshot=snapshot,
                suggested_increase_usd=suggested_increase_usd, lease_held=True)
    job = plain(job)
    if plan.get('pipeline_id') != job.name:
        raise ValueError('Budget request belongs to a different pipeline.')
    selected = [(role, team, member) for role, team in plan['roles'].items()
                for member in team['members'] if member['id'] == member_id]
    if len(selected) != 1 or not isinstance(reason, str) or not reason.strip():
        raise ValueError('Budget request needs one planned member and a concrete exhaustion reason.')
    role, team, member = selected[0]
    if plan['providers'][member['provider_id']]['billing'] != 'metered':
        raise ValueError('Subscription/manual members cannot request USD API budget.')
    revision = plan.get('revision', 0)
    data = load_budget_requests(job)
    for item in data['requests']:
        if item['member_id'] == member_id and item['plan_revision'] == revision and item['reason'] == reason.strip():
            return copy.deepcopy(item)
    snapshot = snapshot or {}
    member_stats = snapshot.get('members', {}).get(member_id, {})
    if member_stats.get('unknown_invocations') or member_stats.get('interrupted_invocations') or snapshot.get('cost_reconciliation_required'):
        raise ValueError('Unknown costs require evidence reconciliation, not a budget-increase request.')
    requested = amount(max(Decimal('.10'), amount(member['budget_usd'])) if suggested_increase_usd is None else suggested_increase_usd)
    if requested <= 0:
        raise ValueError('A budget increase suggestion must be positive.')
    identifier = hashlib.sha256((job.name+'\0'+str(revision)+'\0'+member_id+'\0'+reason.strip()).encode()).hexdigest()[:24]
    item = {'id': identifier, 'member_id': member_id, 'role': role, 'status': 'PENDING', 'plan_revision': revision,
        'reason': reason.strip(), 'created_at': time.time(), 'current_member_cap_usd': member['budget_usd'],
        'current_role_cap_usd': team['budget_usd'], 'total_cap_usd': plan['total_budget_usd'],
        'spent_member_usd': str(amount(member_stats.get('spent_usd', 0))), 'suggested_increase_usd': str(requested)}
    data['requests'].append(item)
    atomic(job/'.budget-requests.json', data)
    return copy.deepcopy(item)
