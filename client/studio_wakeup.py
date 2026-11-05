"""Bounded, session-only local completion-file wakeups.

This module never invokes a CLI, model, API or network. It may ask the injected
service to continue an already authorized job with its original cost/provider
limits. Approvals are memory-only and are never restored from disk on restart.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time

from worker_adapters import ProviderRegistry, TASK_VERSION, decode_completion

STAGE_ROLES = {'gpt_base': 'gameplay', 'claude_vfx': 'vfx', 'gpt_integrate': 'integration', 'research': 'research'}
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
PROVIDER_FIELDS = {'adapter', 'billing', 'capabilities', 'command', 'model', 'settings_ref', 'ready_roles'}
SETTINGS_FIELDS = {'config', 'providers', 'role_routes', 'mcp', 'policy', 'project'}


def _json(path):
    if path.stat().st_size > 1024*1024:
        raise ValueError('A completion/task packet may not exceed one MiB.')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('A completion/task packet must be one JSON object.')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError('The original approval must have a finite positive USD limit.')
    return value


def _settings_digest(settings):
    if not isinstance(settings, dict):
        raise ValueError('A settings snapshot is required for automatic wakeup.')
    return _digest({key: settings[key] for key in SETTINGS_FIELDS if key in settings})


def _registry(settings):
    return ProviderRegistry.from_config({'providers': {name: {key: value for key, value in spec.items() if key in PROVIDER_FIELDS}
        for name, spec in settings.get('providers', {}).items()}, 'role_routes': settings.get('role_routes', {})}, include_defaults=False)


class LocalWakeup:
    def __init__(self, service, root=None, *, state_reader=None, settings_reader=None, request_reader=None,
                 liveness=None, event_callback=None, redactor=None, poll_seconds=1, max_wakes_per_job=8, max_jobs=64,
                 clock=time.time):
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or not 0.1 <= poll_seconds <= 60:
            raise ValueError('Local polling must be between 0.1 and 60 seconds.')
        if isinstance(max_wakes_per_job, bool) or not isinstance(max_wakes_per_job, int) or not 1 <= max_wakes_per_job <= 16:
            raise ValueError('Wakeup attempts must be bounded between one and sixteen per explicit approval.')
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or not 1 <= max_jobs <= 256:
            raise ValueError('The number of session-authorized jobs must be bounded.')
        self.service = service
        self.root = Path(root or service.root).resolve()
        self.state_reader = state_reader or service.get_state
        self.settings_reader = settings_reader or service.get_settings
        self.request_reader = request_reader
        self.liveness = liveness
        self.event_callback = event_callback
        self.redactor = redactor or getattr(service, '_redact', lambda text: str(text))
        self.poll_seconds = poll_seconds
        self.max_wakes_per_job = max_wakes_per_job
        self.max_jobs = max_jobs
        self.clock = clock
        self._entries = {}
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _job(self, job_id):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id):
            raise ValueError('Wakeup requires a plain isolated job ID.')
        job = self.root/'jobs'/job_id
        if job.resolve() != job.absolute() or not (job/'.pipeline_job').is_file():
            raise ValueError('Wakeup requires an initialized nonredirected isolated job.')
        return job

    def _path(self, job, relative):
        path = job/relative
        if path.resolve() != path.absolute() or not path.is_relative_to(job):
            raise ValueError('Redirected wakeup files are not supported.')
        return path

    def remember(self, job_id, request=None, settings=None, *, explicit=False):
        """Remember a validated user-start request; identical automatic calls do not rearm it.

        explicit=True is reserved for a new human start/resume/retry action.
        Credentials and unrelated runner fields are discarded, never persisted.
        """
        self._job(job_id)
        if request is None and self.request_reader:
            request = self.request_reader(job_id)
        if not isinstance(request, dict):
            raise ValueError('A current-session original run request is required.')
        settings = self.settings_reader() if settings is None else settings
        registry = _registry(settings)
        approval = copy.deepcopy(request.get('paid_approval', {}))
        if not isinstance(approval, dict) or not isinstance(approval.get('approved', False), bool):
            raise ValueError('The original paid approval must be an explicit boolean.')
        approved = approval.get('approved', False)
        providers = approval.get('providers', []) if approved else []
        if not isinstance(providers, list) or not all(isinstance(name, str) and name in registry.providers for name in providers) or len(set(providers)) != len(providers):
            raise ValueError('Original approval must name distinct registered providers.')
        permitted = [name for name in providers if registry.get_provider(name).billing == 'metered']
        claimed = request.get('allowed_metered_providers', permitted)
        if not isinstance(claimed, list) or set(claimed) != set(permitted):
            raise ValueError('A wakeup cannot expand the original metered provider scope.')
        policy = request.get('policy', settings.get('policy', {}))
        cap = _amount(approval.get('job_budget_usd', policy.get('job_budget_usd', 3)))
        connection = _amount(approval.get('connection_budget_usd', policy.get('connection_budget_usd', .2)))
        budgets = copy.deepcopy(request.get('provider_budgets', {})) if approved else {}
        if not isinstance(budgets, dict) or any(name not in registry.providers or
            registry.get_provider(name).billing == 'metered' and name not in permitted for name in budgets):
            raise ValueError('Original worker budgets cannot grant additional paid providers.')
        for value in budgets.values():
            if _amount(value) > cap:
                raise ValueError('A worker budget cannot exceed the original task budget.')
        safe = {'approved_paid': approved, 'allowed_metered_providers': permitted, 'provider_budgets': budgets,
                'paid_approval': {'approved': approved, 'providers': permitted, 'job_budget_usd': cap, 'connection_budget_usd': connection},
                'policy': copy.deepcopy(policy)}
        signature = (_digest(safe), _settings_digest(settings))
        with self._lock:
            old = self._entries.get(job_id)
            if old is not None and old['signature'] == signature and not explicit:
                return False
            if old is not None and not explicit:
                # An automatic child may not silently replace old authorization.
                old['disabled'] = 'approval_or_settings_changed'
                return False
            if old is None and len(self._entries) >= self.max_jobs:
                raise ValueError('Current-session wakeup job limit reached.')
            self._entries[job_id] = {'request': safe, 'signature': signature, 'settings_digest': signature[1],
                                     'seen': {}, 'wakes': 0, 'disabled': None, 'error_signatures': set()}
        return True

    def forget(self, job_id, reason='Explicit pause or session authorization revoked'):
        """Call before a human pause; no approval is restored on restart."""
        with self._lock:
            return self._entries.pop(job_id, None) is not None

    def _emit(self, job_id, activity, *, level='info', status=None, stage=None, detail=None):
        event = {'type': 'wakeup', 'job_id': job_id, 'activity': activity, 'text': activity, 'level': level, 'at': self.clock()}
        if status:
            event['status'] = status
        if stage:
            event['stage'] = stage
        if detail:
            event['detail'] = self.redactor(str(detail))[:800]
        if self.event_callback:
            try:
                self.event_callback(copy.deepcopy(event))
            except Exception:
                pass
        return event

    def _row(self, job_id):
        state = self.state_reader(job_id)
        if isinstance(state, dict) and 'jobs' in state:
            return next((row for row in state['jobs'] if row.get('job_id') == job_id), None)
        return state if isinstance(state, dict) and state.get('job_id') == job_id else None

    def _live(self, row):
        stages = row.get('stages', {})
        stages = stages.values() if isinstance(stages, dict) else stages
        live = row.get('running') or row.get('runtime', {}).get('running') or any(stage.get('status') == 'RUNNING' for stage in stages)
        return bool(live or self.liveness and self.liveness(row['job_id']))

    def _unreconciled(self, row):
        checkpoint = row.get('checkpoint') or row.get('saved_checkpoint') or {}
        budget = row.get('budget') or {}
        return bool(budget.get('cost_reconciliation_required') or budget.get('unknown_invocations') or
                    budget.get('interrupted_invocations') or checkpoint.get('accounting', {}).get('cost_reconciliation_required'))

    def _completion(self, job, stage, settings):
        packet_file = self._path(job, '.adapter-state/tasks/'+stage+'/task.json')
        completion_file = self._path(job, '.adapter-state/tasks/'+stage+'/completion.json')
        before = completion_file.stat()
        packet = _json(packet_file)
        completion = _json(completion_file)
        after = completion_file.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError('Completion file changed while being read; write it atomically.')
        if packet.get('version') != TASK_VERSION or packet.get('role') != STAGE_ROLES[stage]:
            raise ValueError('The completion packet does not belong to this waiting stage.')
        expected_workspace = job/('art' if stage == 'claude_vfx' else 'references' if stage == 'research' else 'game')
        if Path(packet.get('inputs', {}).get('workspace', '')).resolve() != expected_workspace:
            raise ValueError('The completion packet points outside its assigned job workspace.')
        frozen = self._path(job, '.workers.json')
        registry = ProviderRegistry.from_config(_json(frozen), include_defaults=False) if frozen.is_file() else _registry(settings)
        provider = registry.get_provider(packet.get('provider_id'))
        if provider.adapter != 'manual-desktop':
            raise ValueError('Only the registered local manual-completion protocol can trigger a file wakeup.')
        plan = registry.plan_task(packet['task_id'], packet['role'], packet['prompt'], inputs=packet['inputs'],
            provider_id=packet['provider_id'], required_capabilities=packet.get('required_capabilities', []), exchange_id=packet['exchange_id'])
        decode_completion(plan, completion)
        return before.st_mtime_ns

    def tick(self):
        """Perform one bounded local scan. No registered session requests means zero scans."""
        if not self._tick_lock.acquire(blocking=False):
            return []
        events = []
        try:
            with self._lock:
                entries = list(self._entries.items())
            for job_id, entry in entries:
                if entry['disabled']:
                    continue
                try:
                    row = self._row(job_id)
                    if row is None or self._live(row):
                        continue
                    job = self._job(job_id)
                    if row.get('status') in ('DONE', 'COMPLETED'):
                        self.forget(job_id)
                        continue
                    checkpoint = row.get('checkpoint') or row.get('saved_checkpoint') or {}
                    paused = row.get('status') in ('PAUSED', 'PAUSING') or checkpoint.get('state') in ('PAUSED', 'PAUSING')
                    if paused or any(self._path(job, name).is_file() for name in ('pause.request', '.controlpause')):
                        entry['disabled'] = 'explicit_pause'
                        events.append(self._emit(job_id, '任务已明确暂停；外部文件不会自动恢复它。', status='PAUSED'))
                        continue
                    if self._unreconciled(row):
                        continue
                    stages = row.get('stages', {})
                    stages = stages.values() if isinstance(stages, dict) else stages
                    waiting = [stage.get('name') for stage in stages if stage.get('status') == 'WAITING_EXTERNAL' and stage.get('name') in STAGE_ROLES]
                    if not waiting:
                        continue
                    settings = self.settings_reader()
                    if _settings_digest(settings) != entry['settings_digest']:
                        entry['disabled'] = 'settings_changed'
                        events.append(self._emit(job_id, '角色、模型、MCP或运行设置已变化；请重新确认后恢复。', status='NEEDS_APPROVAL'))
                        continue
                    ready = []
                    for stage in waiting:
                        completion = self._path(job, '.adapter-state/tasks/'+stage+'/completion.json')
                        if not completion.is_file():
                            continue
                        modified = completion.stat().st_mtime_ns
                        if entry['seen'].get(stage) == modified:
                            continue
                        try:
                            self._completion(job, stage, settings)
                            ready.append((stage, modified))
                        except Exception as error:
                            entry['seen'][stage] = modified
                            events.append(self._emit(job_id, '外部交接文件尚未通过本地协议校验；没有启动工位。', level='error',
                                                     status='INVALID_COMPLETION', stage=stage, detail=error))
                    if not ready:
                        continue
                    if entry['wakes'] >= self.max_wakes_per_job:
                        entry['disabled'] = 'wakeup_limit'
                        events.append(self._emit(job_id, '本次批准的自动唤醒次数已用尽；请检查任务后明确恢复。', status='LIMIT_REACHED'))
                        continue
                    # Recheck pause/liveness/settings immediately before dispatch.
                    current = self._row(job_id)
                    if current is None or self._live(current) or any(self._path(job, name).is_file() for name in ('pause.request', '.controlpause')):
                        continue
                    current_checkpoint = current.get('checkpoint') or current.get('saved_checkpoint') or {}
                    if current.get('status') in ('PAUSED', 'PAUSING') or current_checkpoint.get('state') in ('PAUSED', 'PAUSING'):
                        entry['disabled'] = 'explicit_pause'
                        continue
                    if self._unreconciled(current):
                        continue
                    if _settings_digest(self.settings_reader()) != entry['settings_digest']:
                        entry['disabled'] = 'settings_changed'
                        continue
                    with self._lock:
                        if self._entries.get(job_id) is not entry or entry['disabled']:
                            continue
                        for stage, modified in ready:
                            entry['seen'][stage] = modified
                        entry['wakes'] += 1
                        original = copy.deepcopy(entry['request'])
                        try:
                            result = self.service.start_job(job_id, request=original, paid_approval=original['paid_approval'],
                                provider_budgets=original['provider_budgets'])
                        except Exception as error:
                            entry['disabled'] = 'wakeup_failed'
                            events.append(self._emit(job_id, '自动唤醒未成功，已停止重复启动；请查看原因后明确恢复。', level='error', status='WAKE_FAILED', detail=error))
                        else:
                            events.append(self._emit(job_id, '外部交接文件已就绪，按本会话原批准继续同一任务。',
                                status='ALREADY_RUNNING' if isinstance(result, dict) and result.get('already_running') else 'RESUMING'))
                except Exception as error:
                    signature = self.redactor(str(error))
                    if signature not in entry['error_signatures']:
                        entry['error_signatures'].add(signature)
                        events.append(self._emit(job_id, '本地唤醒检查失败；没有启动新工位。', level='error', status='SCAN_ERROR', detail=error))
                    if len(entry['error_signatures']) >= 8:
                        entry['disabled'] = 'scan_errors'
        finally:
            self._tick_lock.release()
        return events

    def start(self):
        """Start one stoppable local polling thread; never arms a job itself."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            def loop():
                while not self._stop.is_set():
                    self.tick()
                    self._stop.wait(self.poll_seconds)
            self._thread = threading.Thread(target=loop, name='studio-local-wakeup', daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=2):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0, min(60, timeout)))
        return not bool(thread and thread.is_alive())
