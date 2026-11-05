"""Local AI Studio business service. No HTTP server or automatic model calls.

A transport supplied by the application executes authorized jobs. This module
owns settings, independent job controls, sanitized state, and real log updates.
Credentials belong to the application's private session/OS store, never here.
"""
from __future__ import annotations

import copy
from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from typing import Any, Mapping
import urllib.parse
import uuid

from worker_adapters import ADAPTERS, ROLES, ProviderRegistry

STAGE_ROLES = {'gpt_base': 'gameplay', 'claude_vfx': 'vfx', 'research': 'research', 'gpt_integrate': 'integration'}
JOB_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
SECRET_KEY = re.compile(r'(?i)(?:api[_-]?key|auth[_-]?token|access[_-]?token|password|secret|authorization|credentials)')
ENV_REF = re.compile(r'\$\{[A-Za-z_][A-Za-z0-9_]*\}')
DEFAULT_POLICY = {'token_saving': True, 'auto_retry': False, 'max_retries': 1, 'max_revisions': 1,
                  'job_budget_usd': 3.0, 'connection_budget_usd': 0.2, 'autosave': True, 'notify': True}
RUNTIME_CONFIG_FIELDS = {'approved_paid', 'approve_paid', 'paid_approval', 'allowed_metered_providers',
                         'provider_budgets', 'metered_approval', 'session_secrets'}


class ServiceError(ValueError):
    pass


def _load(path: Path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else copy.deepcopy(default)


def _amount(value, label: str, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ServiceError(f'{label} must be a finite nonnegative amount.')
    return float(value)


def _validate_no_secrets(value, key=''):
    if SECRET_KEY.search(key) and key not in ('secret_env', 'credential_env', 'credential_env_refs'):
        if isinstance(value, str) and ENV_REF.search(value):
            # Headers may contain a literal Bearer prefix plus an environment reference.
            remainder = ENV_REF.sub('', value).strip()
            if remainder not in ('', 'Bearer'):
                raise ServiceError('Sensitive settings must use environment references only.')
        elif value not in (None, ''):
            raise ServiceError('Use the private credential endpoint; plaintext secrets cannot be saved in settings.')
    if isinstance(value, dict):
        for child_key, child in value.items():
            _validate_no_secrets(child, str(child_key))
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_no_secrets(child)
    elif isinstance(value, str):
        if re.search(r'\bsk-[A-Za-z0-9_-]{12,}\b', value) or (re.search(r'(?i)\bbearer\s+\S+', value) and not ENV_REF.search(value)):
            raise ServiceError('Settings contain a credential-shaped value; save credentials through the private endpoint.')
        if '://' in value:
            parsed = urllib.parse.urlsplit(value)
            if parsed.username or parsed.password:
                raise ServiceError('URLs cannot contain embedded credentials.')
            for name, child in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                if SECRET_KEY.search(name) or name.lower() in ('token', 'sig', 'signature'):
                    _validate_no_secrets(child, 'secret')


class StudioService:
    def __init__(self, root: str | Path | None = None, *, transport=None, redactor=None, clock=time.time):
        self.root = Path(root or Path(__file__).parent).resolve()
        self.transport = transport
        self.redactor = redactor
        self.clock = clock
        self.config_path = self.root/'config.local.json'
        self.mcp_path = self.root/'mcp.local.json'
        self.database_path = self.root/'.control'/'state.sqlite3'
        self._settings_lock = threading.RLock()
        self._guard = threading.RLock()
        self._job_locks = {}
        self._threads = {}
        self._runtime = {}
        self._events = []
        self._event_sequence = 0
        self._log_seen = {}
        self._log_changes = []
        self._connections = {}
        self._saved_events = {}

    @property
    def supported_adapters(self):
        return sorted(ADAPTERS)

    def _inside(self, path: Path) -> Path:
        absolute = path.absolute()
        if path.resolve() != absolute or not absolute.is_relative_to(self.root):
            raise ServiceError('The requested path is outside the isolated studio or redirected.')
        return absolute

    def _atomic(self, path: Path, data):
        self._inside(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _config(self):
        config = _load(self.config_path, {})
        if not isinstance(config, dict):
            raise ServiceError('Studio configuration must be a JSON object.')
        return config

    def _workers_path(self, config=None):
        config = self._config() if config is None else config
        path = Path(config.get('workers_file', 'workers.local.json'))
        return self._inside(path if path.is_absolute() else self.root/path)

    def _registry(self, config=None):
        path = self._workers_path(config)
        return ProviderRegistry.from_config(_load(path, None), include_defaults=not path.is_file())

    def _lock_for(self, job_id):
        with self._guard:
            return self._job_locks.setdefault(job_id, threading.RLock())

    def _job(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ServiceError('Job ID must contain 1-64 letters, digits, underscores or hyphens.')
        job = self._inside(self.root/'jobs'/job_id)
        if not (job/'.pipeline_job').is_file():
            raise ServiceError('This is not an initialized isolated job.')
        for folder in ('game', 'art', 'references', 'delivery'):
            self._inside(job/folder)
        return job

    def _redact(self, text):
        text = str(text)
        if self.redactor:
            text = self.redactor(text)
        elif self.transport and callable(getattr(self.transport, 'redact', None)):
            text = self.transport.redact(text)
        text = re.sub(r'(?i)(\bBearer\s+)[A-Za-z0-9._~+/=\-]+', r'\1[REDACTED]', text)
        text = re.sub(r'(?i)(\b(?:sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&\s"<>]+', r'\1[REDACTED]', text)
        text = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}\b', '[REDACTED]', text)
        text = re.sub(r'(?i)(["\']?(?:[A-Z_]*API_KEY|[A-Z_]*AUTH_TOKEN|password|secret)["\']?\s*[:=]\s*)["\']?[^\s,"\'}]+', r'\1[REDACTED]', text)
        return text

    def _public(self, value, key=''):
        if key.lower() in ('thinking', 'reasoning', 'analysis'):
            return None
        if SECRET_KEY.search(key) and key not in ('secret_env', 'credential_env', 'credential_env_refs'):
            if isinstance(value, dict) and set(value) <= {'configured', 'remembered', 'source', 'status'}:
                return dict(value)
            if isinstance(value, str) and ENV_REF.search(value):
                return value
            return None
        if isinstance(value, dict):
            output = {}
            for name, child in value.items():
                safe = self._public(child, str(name))
                if safe is not None:
                    output[name] = safe
            return output
        if isinstance(value, (list, tuple)):
            return [self._public(child) for child in value]
        return self._redact(value) if isinstance(value, str) else value

    def _policy(self, config):
        policy = {**DEFAULT_POLICY, **config.get('studio_policy', {})}
        policy['auto_retry'] = config.get('auto_qa_repair_enabled', policy['auto_retry'])
        revisions = config.get('auto_qa_repair_max_attempts', policy['max_revisions'])
        policy['max_retries'] = policy['max_revisions'] = revisions
        policy['job_budget_usd'] = config.get('worker_budget_usd_per_job', config.get('claude_budget_usd_per_job', policy['job_budget_usd']))
        return policy

    def _validated_policy(self, config, supplied=None):
        supplied = supplied or {}
        if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_POLICY):
            raise ServiceError('Unknown run policy.')
        policy = {**self._policy(config), **supplied}
        for field in ('token_saving', 'auto_retry', 'autosave', 'notify'):
            if not isinstance(policy[field], bool):
                raise ServiceError(f'{field} must be true or false.')
        revisions = supplied.get('max_revisions', supplied.get('max_retries', policy['max_revisions']))
        if isinstance(revisions, bool) or not isinstance(revisions, int) or not 0 <= revisions <= 2:
            raise ServiceError('Automatic QA revisions must be between zero and two.')
        policy['max_retries'] = policy['max_revisions'] = revisions
        policy['job_budget_usd'] = _amount(policy['job_budget_usd'], 'Job budget', positive=True)
        policy['connection_budget_usd'] = _amount(policy['connection_budget_usd'], 'Connection budget', positive=True)
        return policy

    def _credential_status(self):
        method = getattr(self.transport, 'credentials_status', None) if self.transport else None
        return self._public(method()) if callable(method) else {}

    def get_settings(self):
        with self._settings_lock:
            config = self._config()
            registry = self._registry(config)
            workers = registry.to_config()
            provider_settings = config.get('provider_settings', {})
            providers = {name: {**spec, 'settings': self._public(provider_settings.get(name, {}))}
                         for name, spec in workers['providers'].items()}
            return {'config': self._public(config), 'providers': providers, 'role_routes': workers['role_routes'],
                    'mcp': self.get_mcp(), 'policy': self._policy(config), 'project': self._public(config.get('project', {})),
                    'available_adapters': self.supported_adapters, 'supported_adapters': self.supported_adapters,
                    'credentials': self._credential_status()}

    def _provider_specs(self, providers, config):
        if not isinstance(providers, dict):
            raise ServiceError('Providers must be an object keyed by provider ID.')
        clean = {}
        settings = copy.deepcopy(config.get('provider_settings', {}))
        registry_fields = {'adapter', 'billing', 'capabilities', 'command', 'model', 'settings_ref', 'ready_roles'}
        ui_fields = {'settings', 'endpoint', 'base_url', 'name'}
        for provider_id, spec in providers.items():
            if not isinstance(spec, dict) or set(spec) - (registry_fields | ui_fields):
                raise ServiceError('Unknown provider setting.')
            private_settings = copy.deepcopy(spec.get('settings', settings.get(provider_id, {})))
            if not isinstance(private_settings, dict):
                raise ServiceError('Provider settings must be a nonsecret JSON object.')
            for field in ('endpoint', 'base_url', 'name'):
                if field in spec:
                    private_settings[field] = spec[field]
            _validate_no_secrets(private_settings)
            for field in ('endpoint', 'base_url'):
                if field in private_settings and private_settings[field]:
                    endpoint = urllib.parse.urlsplit(private_settings[field])
                    if endpoint.scheme != 'https' or not endpoint.netloc or endpoint.username or endpoint.password:
                        raise ServiceError('Provider API endpoints must use HTTPS without embedded credentials.')
            settings[provider_id] = private_settings
            clean[provider_id] = {key: copy.deepcopy(value) for key, value in spec.items() if key in registry_fields}
        config['provider_settings'] = settings
        return clean

    def save_settings(self, patch):
        if not isinstance(patch, dict) or set(patch) - {'config', 'providers', 'role_routes', 'policy', 'project', 'mcp'}:
            raise ServiceError('Unknown settings section.')
        _validate_no_secrets(patch)
        with self._settings_lock:
            config = self._config()
            workers = self._registry(config).to_config()
            if 'config' in patch:
                if not isinstance(patch['config'], dict):
                    raise ServiceError('Config patch must be an object.')
                if any(name.startswith('_') or name in RUNTIME_CONFIG_FIELDS for name in patch['config']):
                    raise ServiceError('Run authorization and private runtime fields cannot be persisted in settings.')
                config.update(copy.deepcopy(patch['config']))
            if 'providers' in patch:
                workers['providers'].update(self._provider_specs(patch['providers'], config))
            if 'role_routes' in patch:
                if not isinstance(patch['role_routes'], dict):
                    raise ServiceError('Role routes must be an object.')
                workers['role_routes'].update(copy.deepcopy(patch['role_routes']))
            registry = ProviderRegistry.from_config(workers, include_defaults=False)
            policy = self._validated_policy(config, patch.get('policy'))
            config.update(studio_policy=policy, auto_qa_repair_enabled=policy['auto_retry'],
                          auto_qa_repair_max_attempts=policy['max_revisions'], worker_budget_usd_per_job=policy['job_budget_usd'])
            if 'project' in patch:
                if not isinstance(patch['project'], dict):
                    raise ServiceError('Project settings must be an object.')
                config['project'] = {**config.get('project', {}), **copy.deepcopy(patch['project'])}
            mcp = self._mcp_from_entries(patch['mcp']) if 'mcp' in patch else None
            workers_path = self._workers_path(config)
            config['workers_file'] = str(workers_path)
            _validate_no_secrets(config)
            self._atomic(workers_path, registry.to_config())
            if mcp is not None:
                self._atomic(self.mcp_path, mcp)
                config['mcp_file'] = str(self.mcp_path)
            self._atomic(self.config_path, config)
        return self.get_settings()

    def add_provider(self, provider_id, provider):
        if provider_id in self.get_settings()['providers']:
            raise ServiceError('That provider ID is already registered.')
        return self.save_settings({'providers': {provider_id: provider}})

    def update_provider(self, provider_id, provider):
        current = self.get_settings()['providers'].get(provider_id)
        if current is None:
            raise ServiceError('Unknown provider.')
        return self.save_settings({'providers': {provider_id: {**current, **provider}}})

    def remove_provider(self, provider_id):
        with self._settings_lock:
            config = self._config()
            workers = self._registry(config).to_config()
            if provider_id not in workers['providers']:
                raise ServiceError('Unknown provider.')
            if any(provider_id == binding or isinstance(binding, list) and provider_id in binding
                   for binding in workers['role_routes'].values()):
                raise ServiceError('Bind its roles to another explicit provider before removing it.')
            del workers['providers'][provider_id]
            config.get('provider_settings', {}).pop(provider_id, None)
            self._atomic(self._workers_path(config), ProviderRegistry.from_config(workers, include_defaults=False).to_config())
            self._atomic(self.config_path, config)
        return self.get_settings()

    def bind_role(self, role, provider_id):
        selected = self._registry().select(role, provider_id)
        return self.save_settings({'role_routes': selected.to_config()['role_routes']})

    def _validate_mcp(self, server_id, entry):
        if not isinstance(server_id, str) or not JOB_ID.fullmatch(server_id):
            raise ServiceError('MCP server IDs must be simple identifiers.')
        if not isinstance(entry, dict) or set(entry) - {'name', 'enabled', 'roles', 'config'}:
            raise ServiceError('Unknown MCP entry field.')
        enabled = entry.get('enabled', True)
        roles = entry.get('roles', list(ROLES))
        config = copy.deepcopy(entry.get('config', {}))
        if not isinstance(enabled, bool) or not isinstance(roles, list) or not roles or not all(isinstance(role, str) for role in roles) or len(set(roles)) != len(roles) or set(roles) - ROLES:
            raise ServiceError('MCP enabled/roles values are invalid.')
        if not isinstance(config, dict) or set(config) - {'command', 'args', 'env', 'url', 'headers', 'type', 'transport', 'startup_timeout_sec', 'tool_timeout_sec'}:
            raise ServiceError('Unknown MCP transport setting.')
        _validate_no_secrets(config)
        stdio = 'command' in config
        if stdio == ('url' in config):
            raise ServiceError('Configure exactly one MCP command or URL.')
        if stdio:
            if not isinstance(config['command'], str) or not config['command'].strip():
                raise ServiceError('MCP command must be a nonempty executable string.')
            if not isinstance(config.get('args', []), list) or not all(isinstance(arg, str) for arg in config.get('args', [])):
                raise ServiceError('MCP args must be a string array.')
            if not isinstance(config.get('env', {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in config.get('env', {}).items()):
                raise ServiceError('MCP env must use strings and secret environment references.')
        else:
            if not isinstance(config['url'], str):
                raise ServiceError('MCP URL must be a string.')
            parsed = urllib.parse.urlsplit(config['url'])
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or (parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1')):
                raise ServiceError('MCP URLs require HTTPS, or HTTP on localhost, without credentials.')
        for flag in ('GODOT_MCP_DISABLE_SECURITY', 'GODOT_MCP_DISABLE_ELICITATION'):
            if str(config.get('env', {}).get(flag, '')).lower() in ('true', '1'):
                raise ServiceError('MCP security/confirmation bypass flags cannot be enabled here.')
        return {'id': server_id, 'name': str(entry.get('name', server_id)), 'enabled': enabled,
                'roles': roles, 'config': config}

    def _mcp_from_entries(self, entries):
        if not isinstance(entries, list):
            raise ServiceError('MCP entries must be an array.')
        output = {}
        for item in entries:
            if not isinstance(item, dict) or 'id' not in item:
                raise ServiceError('Each MCP entry requires an ID.')
            entry = self._validate_mcp(item['id'], {key: value for key, value in item.items() if key != 'id'})
            if entry['id'] in output:
                raise ServiceError('Duplicate MCP server ID.')
            output[entry['id']] = {'name': entry['name'], 'enabled': entry['enabled'], 'roles': entry['roles'], **entry['config']}
        return {'mcpServers': output}

    def get_mcp(self):
        data = _load(self.mcp_path, None)
        if data is None:
            config = self._config()
            entries = []
            if config.get('godot_mcp_script'):
                entries.append({'id': 'godot', 'name': 'Godot MCP', 'enabled': True, 'roles': ['gameplay', 'integration'],
                    'config': {'command': config.get('node_command', ['node'])[0], 'args': [config['godot_mcp_script']],
                               'env': {'GODOT_PATH': config.get('godot_path', ''), 'GODOT_MCP_STRICT': 'true'}}})
            command = config.get('blender_mcp_command')
            if command:
                entries.append({'id': 'blender', 'name': 'Blender MCP', 'enabled': True, 'roles': ['vfx'],
                    'config': {'command': command[0], 'args': command[1:] + config.get('blender_mcp_args', []),
                               'env': {'BLENDER_HOST': '127.0.0.1', 'BLENDER_PORT': str(config.get('blender_port', 9876)), 'BLENDER_MCP_SAFE_MODE': '1'}}})
            return self._public(entries)
        entries = []
        for server_id, spec in data.get('mcpServers', {}).items():
            config = {key: value for key, value in spec.items() if key not in ('name', 'enabled', 'roles')}
            entries.append(self._validate_mcp(server_id, {'name': spec.get('name', server_id), 'enabled': spec.get('enabled', True),
                                                        'roles': spec.get('roles', list(ROLES)), 'config': config}))
        return self._public(entries)

    def save_mcp_entry(self, server_id, entry):
        item = self._validate_mcp(server_id, entry)
        entries = [previous for previous in self.get_mcp() if previous['id'] != server_id] + [item]
        return self.save_settings({'mcp': entries})

    def remove_mcp_entry(self, server_id):
        entries = self.get_mcp()
        if not any(item['id'] == server_id for item in entries):
            raise ServiceError('Unknown MCP server.')
        return self.save_settings({'mcp': [item for item in entries if item['id'] != server_id]})

    def import_mcp_json(self, value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ServiceError('MCP import must be JSON.') from None
        if not isinstance(value, dict) or set(value) != {'mcpServers'} or not isinstance(value['mcpServers'], dict):
            raise ServiceError('MCP import requires one mcpServers object.')
        entries = {item['id']: item for item in self.get_mcp()}
        for server_id, spec in value['mcpServers'].items():
            if not isinstance(spec, dict):
                raise ServiceError('Each imported MCP server must be an object.')
            config = {key: child for key, child in spec.items() if key not in ('name', 'enabled', 'roles')}
            entries[server_id] = self._validate_mcp(server_id, {'name': spec.get('name', server_id), 'enabled': spec.get('enabled', True),
                                                             'roles': spec.get('roles', list(ROLES)), 'config': config})
        return self.save_settings({'mcp': list(entries.values())})

    def init_job(self, job_id, *, goal=None, budget_usd=None, task_patch=None):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ServiceError('Invalid isolated job ID.')
        with self._lock_for(job_id):
            from studio_pipeline_delete import reject_deleted_id
            reject_deleted_id(self.root,job_id)
            job = self._inside(self.root/'jobs'/job_id)
            self._inside(self.database_path)
            self._inside(self.root/'templates'/'project.godot')
            if job.exists():
                raise ServiceError('That job already exists; its files will not be overwritten.')
            task = _load(self.root/'example-task.json', {'goal': 'Isolated AI Studio demo'})
            if task_patch:
                if not isinstance(task_patch, dict) or set(task_patch) - {'kind','software','goal', 'game', 'vfx', 'reference_policy', 'delivery'}:
                    raise ServiceError('Unknown task patch field.')
                _validate_no_secrets(task_patch)
                task.update(copy.deepcopy(task_patch))
            if task.get('kind','fan-wave-demo') not in ('fan-wave-demo','custom-game','software'):
                raise ServiceError('请选择扇气验证模板、自定义 Godot 游戏或软件项目。')
            task['job_id'] = job_id
            if goal is not None:
                if not isinstance(goal, str) or not goal.strip():
                    raise ServiceError('A nonempty project goal is required.')
                task['goal'] = goal
            if budget_usd is not None:
                task['budget_usd'] = _amount(budget_usd, 'Job budget', positive=True)
            job.mkdir(parents=True, exist_ok=False)
            for name in ('game', 'art', 'references', 'delivery'):
                (job/name).mkdir()
            (job/'.pipeline_job').write_text('isolated-demo-v1', encoding='ascii')
            self._atomic(job/'task.json', task)
            template = self.root/'templates'/'project.godot'
            if template.is_file():
                shutil.copyfile(template, job/'game'/'project.godot')
            else:
                (job/'game'/'project.godot').write_text('config_version=5\n', encoding='utf-8')
            (job/'art'/'CLAUDE.md').write_text('Only this isolated art directory may be edited. Never inspect credentials or original projects.\n', encoding='utf-8')
            (job/'game'/'AGENTS.md').write_text('Only this isolated game directory may be edited. Preserve independent acceptance tests.\n', encoding='utf-8')
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.database_path, timeout=10)) as database, database:
                database.execute('CREATE TABLE IF NOT EXISTS stages(job TEXT,name TEXT,status TEXT,attempts INTEGER DEFAULT 0,detail TEXT DEFAULT "",updated REAL,PRIMARY KEY(job,name))')
                database.executemany('INSERT INTO stages(job,name,status,updated) VALUES(?,?,?,?)',
                    [(job_id, stage, status, self.clock()) for stage, status in [('gpt_base', 'READY'), ('claude_vfx', 'READY'), ('research', 'DORMANT'), ('gpt_integrate', 'DORMANT'), ('qa', 'DORMANT')]])
        return self.get_state(job_id)['jobs'][0]

    create_job = init_job

    def update_task(self,job_id,*,goal=None,budget_usd=None,task_patch=None):
        """Save a new legacy task in a retained, fully cancelled pipeline."""
        from decimal import Decimal
        from studio_task_cycles import pipeline_edit_lease,load as cycle_load,atomic as cycle_atomic,finish_new_task
        job=self._job(job_id)
        if (job/'pipeline.v2.json').exists() or (job/'pipeline.v3.json').exists():
            raise ServiceError('Use this pipeline generation’s existing plan-edit endpoint.')
        with self._lock_for(job_id),pipeline_edit_lease(job):
            marker=cycle_load(job/'cancelled.json',{})
            if marker.get('status')!='CANCELLED':raise ServiceError('Save a replacement task only after cancellation is complete.')
            task=copy.deepcopy(_load(job/'task.json',{}))
            patch=task_patch or {}
            if not isinstance(patch,dict) or set(patch)-{'kind','software','goal','game','vfx','reference_policy','delivery'}:raise ServiceError('Unknown new task field.')
            _validate_no_secrets(patch);task.update(copy.deepcopy(patch))
            if goal is not None:
                if not isinstance(goal,str) or not goal.strip():raise ServiceError('A nonempty new task goal is required.')
                task['goal']=goal
            if task.get('kind','fan-wave-demo') not in ('fan-wave-demo','custom-game','software'):raise ServiceError('Unsupported new task kind.')
            cap=_amount(task.get('budget_usd',3) if budget_usd is None else budget_usd,'Task budget',positive=True)
            ledger=job/'.adapter-state/budget.json'
            if ledger.is_file():
                saved=cycle_load(ledger)
                from adapter_budget import BudgetLedger
                BudgetLedger(ledger,saved.get('total_cap_usd')).snapshot()
                entries=saved['invocations'].values()
                floor=sum((Decimal(str(row.get('cost_usd',0))) if row.get('state')=='reconciled' else Decimal(str(row.get('reserved_cap_usd',0))) for row in entries),Decimal(0))
                if Decimal(str(cap))<floor:raise ServiceError('The new budget cannot be below historical spend/reservations.')
                if any(row['state']!='reconciled' for row in saved['invocations'].values()) and Decimal(str(cap))<Decimal(saved['total_cap_usd']):raise ServiceError('Reconcile unknown/interrupted fees before lowering the frozen total cap.')
                if Decimal(saved['total_cap_usd'])!=Decimal(str(cap)):
                    saved['total_cap_usd']=str(cap);cycle_atomic(ledger,saved)
            epoch=marker.get('task_epoch',0)+1
            if task.get('task_epoch',0) not in (marker.get('task_epoch',0),epoch):raise ServiceError('Task replacement epoch is invalid.')
            task.update(job_id=job_id,budget_usd=cap,task_epoch=epoch)
            cycle_atomic(job/'task.json',task)
            finish_new_task(self,job,new_epoch=epoch)
        return self.get_state(job_id)['jobs'][0]

    def _stage_rows(self, job_id):
        if not self.database_path.is_file():
            return []
        try:
            with closing(sqlite3.connect('file:' + self.database_path.as_posix() + '?mode=ro', uri=True, timeout=1)) as database:
                database.row_factory = sqlite3.Row
                return [dict(row) for row in database.execute('SELECT * FROM stages WHERE job=? ORDER BY name', (job_id,))]
        except sqlite3.Error:
            return []

    def _events_for(self, job_id, event):
        if not isinstance(event, dict):
            return
        if event.get('type') in ('reasoning', 'thinking') or event.get('activity') in ('reasoning', 'thinking'):
            return
        safe = self._public(event)
        with self._guard:
            self._event_sequence += 1
            safe.update(job_id=job_id, sequence=self._event_sequence)
            safe.setdefault('at', self.clock())
            self._events.append(safe)
            self._events = self._events[-300:]
            runtime = self._runtime.setdefault(job_id, {})
            runtime['last_activity_at'] = safe['at']
            if safe.get('type') == 'runner' and safe.get('status'):
                runtime['status'] = safe['status'].upper()
            if safe.get('type') == 'connection' and safe.get('provider_id'):
                self._connections[safe['provider_id']] = safe

    def _log_state(self, job):
        events_file = job/'.studio-events.jsonl'
        if events_file.is_file():
            self._inside(events_file)
            with events_file.open('rb') as stream:
                stream.seek(max(0, events_file.stat().st_size - 65536))
                data = stream.read().decode('utf-8', errors='replace')
            events = []
            for line in data.splitlines()[-250:]:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get('type') in ('reasoning', 'thinking') or event.get('activity') in ('reasoning', 'thinking'):
                    continue
                events.append({**self._public(event), 'job_id': job.name})
            self._saved_events[job.name] = events
        candidates = []
        if not events_file.is_file():
            # Historical SDK stdout may include private reasoning. Only expose
            # the explicit final summary and completed tool names, never its log.
            for workspace in ('game', 'art', 'references'):
                folder = job/workspace/'.pipeline'
                for path in list(folder.glob('*.result.json')) + list(folder.glob('*.tools.json')):
                    try:
                        self._inside(path)
                        stat = path.stat()
                        relative = str(path.relative_to(self.root))
                        signature = (stat.st_mtime_ns, stat.st_size)
                        if self._log_seen.get(relative) == signature:
                            continue
                        content = _load(path, {})
                        text = content.get('summary') or ', '.join(content.get('successful_tool_names', []))
                        if not text:
                            continue
                        self._log_seen[relative] = signature
                        stage = path.name.split('-')[0]
                        self._log_changes.append({'job_id': job.name, 'stage': stage, 'path': relative,
                                                  'bytes': stat.st_size, 'modified_at': stat.st_mtime, 'tail': self._redact(text)})
                    except (OSError, ValueError, TypeError, AttributeError):
                        continue
        candidates.extend(path for path in (job/'delivery').glob('*.log')
                          if path.name.startswith(('godot', 'base_smoke', 'integration_smoke', 'preview-record')))
        for path in candidates:
            try:
                self._inside(path)
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                relative = str(path.relative_to(self.root))
                if self._log_seen.get(relative) == signature:
                    continue
                self._log_seen[relative] = signature
                with path.open('rb') as stream:
                    stream.seek(max(0, stat.st_size - 8192))
                    tail = stream.read().decode('utf-8', errors='replace')
                stage = 'gpt_base' if 'base' in path.name else 'gpt_integrate' if 'integration' in path.name else 'qa'
                lines = [self._redact(line) for line in tail.splitlines()[-12:]]
                self._log_changes.append({'job_id': job.name, 'stage': stage, 'path': relative, 'bytes': stat.st_size,
                                          'modified_at': stat.st_mtime, 'tail': '\n'.join(lines)[-3000:]})
            except (OSError, ServiceError):
                continue
        self._log_changes = self._log_changes[-100:]

    def _budget(self, job):
        reader = getattr(self.transport, 'get_budget', None) if self.transport else None
        if callable(reader):
            try:
                return self._public(reader(job.name))
            except Exception:
                pass
        try:
            ledger = _load(self._inside(job/'.adapter-state'/'budget.json'), None)
            report = _load(self._inside(job/'delivery'/'cost_report.json'), None)
        except (OSError, ValueError):
            return {'currency': 'USD', 'spent_usd': None, 'remaining_usd': None, 'cost_known': False,
                    'cost_reconciliation_required': True, 'detail': 'The local cost record could not be read.'}
        if isinstance(ledger, dict):
            return self._public(ledger)
        if isinstance(report, dict):
            return self._public(report)
        return {'currency': 'USD', 'limit_usd': self._policy(self._config())['job_budget_usd'],
                'spent_usd': None, 'remaining_usd': None, 'cost_known': False}

    def list_jobs(self):
        return self.get_state()['jobs']

    def get_state(self, job_id=None):
        if job_id is None:
            directories = sorted((self.root/'jobs').glob('*')) if (self.root/'jobs').is_dir() else []
            jobs = []
            for path in directories:
                if path.is_dir() and (path/'.pipeline_job').is_file():
                    try:
                        jobs.append(self._job(path.name))
                    except ServiceError:
                        continue
        else:
            jobs = [self._job(job_id)]
        current_registry = self._registry()
        output = []
        for job in jobs:
            self._log_state(job)
            registry = current_registry
            snapshot = _load(job/'.workers.json', None)
            if snapshot is not None:
                registry = ProviderRegistry.from_config(snapshot, include_defaults=False)
            stages = []
            for row in self._stage_rows(job.name):
                role = STAGE_ROLES.get(row['name'])
                candidates = registry.role_routes.get(role, ()) if role else ()
                provider_id = candidates[0] if len(candidates) == 1 else None
                spec = registry.providers.get(provider_id)
                stages.append({**row, 'detail': self._redact(row.get('detail', '')), 'role': role,
                               'provider_id': provider_id, 'provider': provider_id,
                               'provider_candidates': list(candidates), 'adapter': spec.adapter if spec else None,
                               'billing': spec.billing if spec else 'local', 'model': spec.model if spec else None})
            with self._guard:
                runtime = copy.deepcopy(self._runtime.get(job.name, {}))
                running = bool(self._threads.get(job.name) and self._threads[job.name].is_alive())
                visible_events = self._saved_events.get(job.name, []) + [event for event in self._events if event['job_id'] == job.name]
            getter = getattr(self.transport, 'get_job_runtime', None) if self.transport else None
            if callable(getter):
                try:
                    runtime.update(self._public(getter(job.name)) or {})
                except Exception:
                    pass
            running=running or bool(runtime.get('running'))
            for stage in stages:
                actual = [event for event in visible_events if event.get('stage') == stage['name']]
                historic = [item['tail'] for item in self._log_changes if item['job_id'] == job.name and item.get('stage') == stage['name']]
                stage['log_tail'] = (historic + [event.get('text', '') for event in actual if event.get('text')])[-80:]
                if actual:
                    stage['activity'] = actual[-1].get('text') or actual[-1].get('activity')
                    stage['latest_tool_event'] = next((event.get('text') for event in reversed(actual)
                                                      if event.get('activity') in ('tool', 'tool_call', 'mcp_tool', 'command', 'file')), None)
            checkpoint = _load(job/'checkpoint'/'metadata.json', None)
            status = runtime.get('status')
            if not running:
                if any(stage['name'] == 'qa' and stage['status'] == 'DONE' for stage in stages):
                    status = 'DONE'
                elif checkpoint and checkpoint.get('state') == 'PAUSED':
                    status = 'PAUSED'
                elif any(stage['status'].startswith('WAITING_') for stage in stages):
                    status = 'WAITING'
                elif any(stage['status'] in ('FAILED', 'BLOCKED', 'BUDGET_STOP', 'INTERRUPTED') for stage in stages):
                    status = 'NEEDS_ATTENTION'
                else:
                    status = status or 'READY'
            output.append({'job_id': job.name, 'status': status or 'RUNNING', 'running': running,
                           'kind':_load(job/'task.json',{}).get('kind','fan-wave-demo'),
                           'goal': self._public(_load(job/'task.json', {}).get('goal', '')),
                           'stages': {stage['name']: stage for stage in stages}, 'stage_rows': stages,
                           'role_routes': registry.to_config()['role_routes'], 'budget': self._budget(job),
                           'checkpoint': self._public(checkpoint), 'saved_checkpoint': self._public(checkpoint),
                           'runtime': self._public(runtime), 'delivery_exists': any((job/'delivery'/name).is_file() for name in ('game_project.zip','source_project.zip'))})
        with self._guard:
            connections = copy.deepcopy(self._connections)
            events = copy.deepcopy(self._events) + [event for group in self._saved_events.values() for event in group]
        getter = getattr(self.transport, 'get_connection_status', None) if self.transport else None
        if callable(getter):
            try:
                connections.update(self._public(getter()) or {})
            except Exception:
                pass
        for provider_id in current_registry.providers:
            connections.setdefault(provider_id, {'status': 'not_checked', 'detail': 'No connection check has been requested.'})
        caps = getattr(self.transport, 'capabilities', {}) if self.transport else {}
        if callable(caps):
            caps = caps()
        capabilities = {'auto_retry': bool(caps.get('auto_retry', getattr(self.transport, 'supports_auto_retry', False))) if isinstance(caps, dict) else False,
                        'pause_resume': True, 'live_events': True}
        running_jobs = [item['job_id'] for item in output if item['running']]
        return {'jobs': output, 'connections': connections, 'credentials': self._credential_status(),
                'app': {'studio_path': str(self.root), 'supported_adapters': self.supported_adapters, 'capabilities': capabilities},
                'running_jobs': running_jobs, 'running_job': running_jobs[0] if running_jobs else None,
                'budgets': {item['job_id']: item['budget'] for item in output},
                'latest_events': [event for event in events if job_id is None or event['job_id'] == job_id][-100:],
                'log_changes': [item for item in self._log_changes if job_id is None or item['job_id'] == job_id][-50:],
                'updated_at': self.clock()}

    def _request(self, action, request, approved_paid, provider_budgets, paid_approval):
        request = copy.deepcopy(request or {})
        _validate_no_secrets(request)
        if not isinstance(request, dict):
            raise ServiceError('Run request must be an object.')
        approval = copy.deepcopy(paid_approval if paid_approval is not None else request.get('paid_approval', {}))
        if not isinstance(approval, dict):
            raise ServiceError('Paid approval must be an object.')
        approved = approval.get('approved', approved_paid)
        if not isinstance(approved, bool):
            raise ServiceError('Paid approval must be explicit true or false.')
        policy = self.get_settings()['policy']
        registry = self._registry()
        selected = approval.get('providers', list((provider_budgets or request.get('provider_budgets', {})).keys())) if approved else []
        if not isinstance(selected, list) or not all(isinstance(name, str) and name in registry.providers for name in selected):
            raise ServiceError('Approval must name known provider IDs.')
        job_cap = _amount(approval.get('job_budget_usd', policy['job_budget_usd']), 'Approved job budget', positive=True)
        connection_cap = _amount(approval.get('connection_budget_usd', policy['connection_budget_usd']), 'Approved connection budget', positive=True)
        caps = copy.deepcopy(provider_budgets if provider_budgets is not None else request.get('provider_budgets', {}))
        if not isinstance(caps, dict) or set(caps) - set(registry.providers):
            raise ServiceError('Provider budgets must name registered providers.')
        caps = {name: min(_amount(value, 'Provider budget', positive=True), job_cap) for name, value in caps.items()}
        allowed = [name for name in selected if registry.get_provider(name).billing == 'metered']
        for name in allowed:
            caps.setdefault(name, job_cap)
        request.update(action=action, approved_paid=approved, allowed_metered_providers=allowed,
                       provider_budgets=caps if approved else {},
                       paid_approval={'approved': approved, 'providers': allowed, 'job_budget_usd': job_cap, 'connection_budget_usd': connection_cap},
                       policy=policy)
        return request

    def _start(self, job_id, action, *, request=None, approved_paid=False, provider_budgets=None, paid_approval=None):
        job=self._job(job_id)
        from studio_task_cycles import ensure_task_startable
        ensure_task_startable(job)
        request = self._request(action, request, approved_paid, provider_budgets, paid_approval)
        if self.transport is None or not callable(getattr(self.transport, 'run_job', None)):
            raise ServiceError('The application runner transport is not configured.')
        with self._lock_for(job_id):
            from studio_pipeline_delete import reject_deleted_id
            reject_deleted_id(self.root,job_id)
            ensure_task_startable(job)
            with self._guard:
                active = self._threads.get(job_id)
                if active and active.is_alive():
                    return {'job_id': job_id, 'status': self._runtime.get(job_id, {}).get('status', 'RUNNING'), 'already_running': True}
                if any(row['name'] == 'qa' and row['status'] == 'DONE' for row in self._stage_rows(job_id)):
                    return {'job_id': job_id, 'status': 'DONE', 'already_completed': True}
                self._runtime[job_id] = {'status': 'RUNNING', 'action': action, 'started_at': self.clock()}
            cancel_event = threading.Event()
            def run():
                try:
                    result = self.transport.run_job(job_id, request, lambda event: self._events_for(job_id, event), cancel_event)
                    status = result.get('status') if isinstance(result, dict) else None
                    with self._guard:
                        if status:
                            self._runtime[job_id]['status'] = str(status).upper()
                        elif self._runtime[job_id].get('status') == 'PAUSING':
                            self._runtime[job_id]['status'] = 'PAUSED'
                        elif isinstance(result, int) and not isinstance(result, bool) and result != 0:
                            self._runtime[job_id]['status'] = 'FAILED'
                        else:
                            self._runtime[job_id]['status'] = 'FINISHED'
                        self._runtime[job_id]['finished_at'] = self.clock()
                except Exception as exc:
                    self._events_for(job_id, {'type': 'runner', 'status': 'failed', 'level': 'error', 'text': self._redact(exc)})
            thread = threading.Thread(target=run, name='studio-' + job_id, daemon=True)
            with self._guard:
                self._threads[job_id] = thread
            thread.start()
        return {'job_id': job_id, 'status': 'RUNNING', 'approved_paid': request['approved_paid'],
                'allowed_metered_providers': request['allowed_metered_providers']}

    def start_job(self, job_id, **kwargs):
        return self._start(job_id, 'start', **kwargs)

    def resume_job(self, job_id, **kwargs):
        return self._start(job_id, 'resume', **kwargs)

    def retry_job(self, job_id, stage, **kwargs):
        self._job(job_id)
        row = next((row for row in self._stage_rows(job_id) if row['name'] == stage), None)
        if row is None or row['status'] not in ('FAILED', 'BLOCKED', 'INTERRUPTED'):
            raise ServiceError('Only an explicitly failed, blocked or interrupted stage can be retried.')
        request = copy.deepcopy(kwargs.pop('request', None) or {})
        request['stage'] = stage
        return self._start(job_id, 'retry', request=request, **kwargs)

    def pause_job(self, job_id, reason='Operator requested pause'):
        job = self._job(job_id)
        with self._lock_for(job_id):
            method = getattr(self.transport, 'request_pause', None) if self.transport else None
            if callable(method):
                method(job_id, reason=reason)
            else:
                self._atomic(job/'pause.request', {'job_id': job_id, 'reason': reason, 'requested_at': self.clock()})
            with self._guard:
                active = self._threads.get(job_id)
                self._runtime.setdefault(job_id, {})['status'] = 'PAUSING' if active and active.is_alive() else 'PAUSED'
        self._events_for(job_id, {'type': 'runner', 'status': self._runtime[job_id]['status'], 'text': 'Pause requested; owned workers will checkpoint before stopping.'})
        return {'job_id': job_id, 'status': self._runtime[job_id]['status']}

    start = start_job
    resume = resume_job
    pause = pause_job
    retry = retry_job

    def check_connections(self, provider_ids=None, *, provider_id=None, test_model=False, approve_paid=False):
        registry = self._registry()
        selected = provider_ids if provider_ids is not None else ([provider_id] if provider_id else list(registry.providers))
        if not isinstance(selected, list):
            raise ServiceError('Connection checks require provider IDs.')
        for name in selected:
            registry.get_provider(name)
        if test_model:
            if approve_paid is not True:
                return {'requires_paid_confirmation': True, 'connection_budget_usd': self.get_settings()['policy']['connection_budget_usd'], 'providers': selected}
            method = getattr(self.transport, 'test_connections', None) if self.transport else None
            if not callable(method):
                raise ServiceError('Paid connection tests are not available in this transport.')
            result = method(selected, limit_usd=self.get_settings()['policy']['connection_budget_usd'])
        else:
            method = getattr(self.transport, 'check_connections', None) if self.transport else None
            if not callable(method):
                raise ServiceError('Local connection checks are not available in this transport.')
            result = method(selected)
        result = self._public(result)
        if isinstance(result, dict):
            with self._guard:
                self._connections.update(result.get('connections', result))
        return result

    def wait_for_job(self, job_id, timeout=0):
        with self._guard:
            thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout=timeout)
        return not bool(thread and thread.is_alive())

    def resolve_open_target(self, job_id, target):
        job = self._job(job_id)
        targets = {'delivery': job/'delivery', 'project': job/'game'/'project.godot',
                   'preview': job/'delivery'/'preview.gif'}
        if target not in targets:
            raise ServiceError('Unknown project output target.')
        return str(self._inside(targets[target]))

    def shutdown(self):
        with self._guard:
            active = [job_id for job_id, thread in self._threads.items() if thread.is_alive()]
        for job_id in active:
            self.pause_job(job_id, 'Studio service shutdown requested a checkpoint')
