"""Independent frozen V2 pipeline/team plans over the local business service.

This module stores plans and routes explicit requests only. It never starts a
model by loading, creating, updating or cloning a plan. Credentials stay in the
application's private store and are never copied into pipeline files.
"""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import threading
import time

from studio_service import StudioService, ServiceError, JOB_ID, DEFAULT_POLICY, _validate_no_secrets, _amount, _load
from worker_adapters import ProviderRegistry, ProviderSpec, ROLES, ROLE_CAPABILITIES, default_config
from studio_v2_budget import TeamBudgetLedger, amount
from studio_v25_changes import (pipeline_edit_lease, recover_plan_update, commit_plan_update,
    load_budget_requests, REQUEST_SCHEMA, REVISION_SCHEMA)

SCHEMA = 'ai-studio-pipeline/v2'
ROLE_STAGES = {'gameplay': 'gpt_base', 'vfx': 'claude_vfx', 'integration': 'gpt_integrate', 'research': 'research'}
CONFIG_FIELDS = frozenset({'codex_model', 'claude_model', 'codex_command', 'claude_command', 'node_command', 'uvx_command',
    'godot_path', 'godot_mcp_script', 'blender_path', 'blender_addon_path', 'blender_mcp_command', 'blender_mcp_args', 'blender_host', 'blender_port',
    'profiles_dir', 'claude_base_url', 'claude_auth', 'claude_network_env', 'codex_network_env', 'codex_disable_request_compression',
    'worker_budget_usd_per_call', 'worker_timeout_seconds', 'claude_max_turns', 'claude_smoke_max_turns', 'pause_grace_seconds',
    'max_claude_calls_per_job', 'max_codex_calls_per_stage', 'max_research_calls', 'generate_preview', 'preview_frames',
    'runtime_private_dir', 'studio_version'})
PLAN_FIELDS = frozenset({'schema', 'pipeline_id', 'kind', 'goal', 'total_budget_usd', 'roles', 'providers',
                         'provider_settings', 'policy', 'mcp', 'config', 'revision', 'task_epoch'})


def _identifier(value, label):
    if not isinstance(value, str) or not JOB_ID.fullmatch(value):
        raise ServiceError(label+' must contain 1-64 letters, digits, underscores or hyphens.')
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(label+' must be a nonempty string.')
    return value.strip()


def _frozen_config(data):
    if not isinstance(data, dict) or set(data)-CONFIG_FIELDS:
        raise ServiceError('Unknown/private runtime configuration cannot be frozen in a plan.')
    _validate_no_secrets(data)
    return copy.deepcopy(data)


def _policy(data, total):
    if not isinstance(data, dict) or set(data)-set(DEFAULT_POLICY):
        raise ServiceError('Unknown pipeline policy.')
    result = {**DEFAULT_POLICY, **copy.deepcopy(data)}
    for name in ('token_saving', 'auto_retry', 'autosave', 'notify'):
        if not isinstance(result[name], bool):
            raise ServiceError(name+' must be true or false.')
    revisions = data.get('max_revisions', data.get('max_retries', result['max_revisions']))
    if isinstance(revisions, bool) or not isinstance(revisions, int) or not 0 <= revisions <= 2:
        raise ServiceError('QA repair policy allows zero, one or two revisions.')
    result['max_retries'] = result['max_revisions'] = revisions
    result['job_budget_usd'] = total
    connection = _amount(result['connection_budget_usd'], 'Connection budget', positive=True)
    if connection > .2:
        raise ServiceError('Connection model probes are limited to at most 0.20 USD per explicit test.')
    result['connection_budget_usd'] = connection
    return result


def normalize_plan(data, defaults=None, *, pipeline_id=None):
    """Validate and freeze a complete plan without running any workers.

    Role/member caps are shared ceilings: their sums need not fit the parent,
    while each individual child cap must fit its parent. Member IDs are unique
    across the line, and creating a line/member has no artificial count quota.
    """
    if not isinstance(data, dict) or set(data)-PLAN_FIELDS:
        raise ServiceError('Unknown V2 pipeline plan field.')
    _validate_no_secrets(data)
    defaults = defaults or {}
    identifier = _identifier(pipeline_id or data.get('pipeline_id'), 'Pipeline ID')
    revision = data.get('revision', 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ServiceError('Plan revision must be a nonnegative integer.')
    if data.get('schema', SCHEMA) != SCHEMA:
        raise ServiceError('Unsupported pipeline plan schema.')
    kind = data.get('kind', 'software')
    if kind not in ('software', 'custom-game', 'fan-wave-demo'):
        raise ServiceError('Choose software, custom-game or fan-wave-demo.')
    goal = _text(data.get('goal', defaults.get('project', {}).get('goal', '')), 'Pipeline goal')
    total = _amount(data.get('total_budget_usd', defaults.get('policy', {}).get('job_budget_usd', 3)), 'Pipeline budget')
    source_config = {key: value for key, value in defaults.get('config', {}).items() if key in CONFIG_FIELDS}
    config = _frozen_config(data.get('config', source_config))
    source = data.get('providers', defaults.get('providers', default_config()['providers']))
    if not isinstance(source, dict) or not source:
        raise ServiceError('Pipeline providers must be a nonempty mapping.')
    providers = {}
    provider_settings = copy.deepcopy(data.get('provider_settings', defaults.get('config', {}).get('provider_settings', {})))
    if not isinstance(provider_settings, dict):
        raise ServiceError('Provider settings must be a nonsecret mapping.')
    for provider_id, original in source.items():
        if not isinstance(original, dict):
            raise ServiceError('Each provider needs a complete nonsecret definition.')
        if set(original)-{'adapter', 'billing', 'capabilities', 'command', 'model', 'settings_ref', 'ready_roles', 'settings'}:
            raise ServiceError('Provider definitions contain unknown fields.')
        spec = {key: copy.deepcopy(value) for key, value in original.items() if key != 'settings'}
        if original.get('settings'):
            provider_settings[provider_id] = copy.deepcopy(original['settings'])
        adapter = spec.get('adapter')
        if not spec.get('model') and adapter in ('codex', 'claude'):
            spec['model'] = config.get(adapter+'_model')
        if adapter in ('codex', 'claude') and spec.get('command', [adapter]) == [adapter] and config.get(adapter+'_command'):
            spec['command'] = copy.deepcopy(config[adapter+'_command'])
        parsed = ProviderSpec.from_config(provider_id, spec)
        providers[provider_id] = parsed.to_config()
        if adapter == 'claude':
            settings = provider_settings.setdefault(provider_id, {})
            if not isinstance(settings, dict):
                raise ServiceError('Provider settings must be a nonsecret object.')
            if not settings.get('endpoint') and config.get('claude_base_url'):
                settings['endpoint'] = config['claude_base_url']
            settings.setdefault('auth', config.get('claude_auth', 'bearer'))
    _validate_no_secrets(provider_settings)
    # Reuse the core transport validator without modifying any global files.
    if any(not isinstance(settings, dict) for settings in provider_settings.values()):
        raise ServiceError('Each provider settings reference must be an object.')
    import urllib.parse
    for settings in provider_settings.values():
        for key in ('endpoint', 'base_url'):
            if settings.get(key):
                url = urllib.parse.urlsplit(settings[key])
                if url.scheme != 'https' or not url.hostname or url.username or url.password:
                    raise ServiceError('Provider endpoints must use HTTPS without credentials.')
    routes = defaults.get('role_routes', default_config()['role_routes'])
    roles = data.get('roles')
    if roles is None:
        roles = {}
        for role in ROLES:
            binding = routes.get(role)
            provider_id = binding[0] if isinstance(binding, list) and binding else binding
            roles[role] = {'budget_usd': total, 'members': [{'id': role+'-1', 'provider_id': provider_id, 'model': None,
                'assignment': ('Research only when a concrete reference request exists.' if role == 'research' else 'Complete the '+role+' part of the stated pipeline goal.'),
                'budget_usd': total, 'required': role != 'research'}]}
    if not isinstance(roles, dict) or set(roles) != ROLES:
        raise ServiceError('A pipeline must configure exactly gameplay, vfx, integration and research teams.')
    parsed_roles = {}
    member_ids = set()
    for role, team in roles.items():
        if not isinstance(team, dict) or set(team)-{'budget_usd', 'members'}:
            raise ServiceError('A role team has only budget_usd and members.')
        cap = _amount(team.get('budget_usd', total), 'Role budget')
        members = team.get('members')
        if not isinstance(members, list) or not members:
            raise ServiceError('Every role must include at least one explicitly configured member.')
        parsed_members = []
        for member in members:
            if not isinstance(member, dict) or set(member)-{'id', 'provider_id', 'model', 'assignment', 'budget_usd', 'required'}:
                raise ServiceError('Unknown member plan field.')
            member_id = _identifier(member.get('id'), 'Member ID')
            if member_id in member_ids:
                raise ServiceError('Member IDs must be unique across the entire pipeline.')
            member_ids.add(member_id)
            provider_id = member.get('provider_id')
            if provider_id not in providers:
                raise ServiceError('Each member must name a frozen registered provider.')
            provider = ProviderSpec.from_config(provider_id, providers[provider_id])
            if not ROLE_CAPABILITIES[role] <= provider.capabilities or role not in provider.ready_roles:
                raise ServiceError('The selected member provider lacks configured role capabilities or readiness.')
            model = member.get('model')
            if model is not None:
                model = _text(model, 'Member model')
                if any(ord(char) < 32 for char in model):
                    raise ServiceError('Member model must be a single-line identifier.')
            assignment = _text(member.get('assignment'), 'Member assignment')
            member_cap = _amount(member.get('budget_usd', cap), 'Member budget') if provider.billing == 'metered' else 0
            if provider.billing == 'metered' and member_cap > cap:
                raise ServiceError('A member cap cannot exceed its shared role cap.')
            required = member.get('required', True)
            if not isinstance(required, bool):
                raise ServiceError('Member required must be true or false.')
            parsed_members.append({'id': member_id, 'provider_id': provider_id, 'model': model,
                                  'assignment': assignment, 'budget_usd': member_cap, 'required': required})
        if not any(providers[member['provider_id']]['billing'] == 'metered' for member in parsed_members):
            cap = 0
        if cap > total:
            raise ServiceError('An API role cap cannot exceed the shared pipeline cap.')
        parsed_roles[role] = {'budget_usd': cap, 'members': parsed_members}
    mcp = copy.deepcopy(data.get('mcp', defaults.get('mcp', [])))
    if not isinstance(mcp, list):
        raise ServiceError('Frozen MCP settings must be an array.')
    server_ids = set()
    canonical_mcp = []
    for entry in mcp:
        if not isinstance(entry, dict) or 'id' not in entry:
            raise ServiceError('Each frozen MCP server needs an ID.')
        _identifier(entry['id'], 'MCP ID')
        if entry['id'] in server_ids:
            raise ServiceError('MCP IDs cannot be duplicated.')
        server_ids.add(entry['id'])
        canonical_mcp.append(StudioService._validate_mcp(None, entry['id'], {key: value for key, value in entry.items() if key != 'id'}))
    task_epoch=data.get('task_epoch',0)
    if isinstance(task_epoch,bool) or not isinstance(task_epoch,int) or task_epoch<0:raise ServiceError('Task epoch must be a nonnegative integer.')
    plan = {'schema': SCHEMA, 'pipeline_id': identifier, 'revision': revision, 'task_epoch':task_epoch, 'kind': kind, 'goal': goal, 'total_budget_usd': total,
            'roles': parsed_roles, 'providers': providers, 'provider_settings': provider_settings,
            'policy': _policy(data.get('policy', defaults.get('policy', {})), total), 'mcp': canonical_mcp, 'config': config}
    _validate_no_secrets(plan)
    json.dumps(plan, allow_nan=False)
    return plan


def budget_caps(plan):
    return ({role: team['budget_usd'] for role, team in plan['roles'].items()},
            {member['id']: {'role': role, 'budget_usd': member['budget_usd']} for role, team in plan['roles'].items() for member in team['members']})


def plan_registry(plan):
    routes = {role: ([member['provider_id'] for member in team['members']] if role == 'research' else team['members'][0]['provider_id'])
              for role, team in plan['roles'].items()}
    routes['research'] = list(dict.fromkeys(routes['research']))
    return ProviderRegistry.from_config({'providers': plan['providers'], 'role_routes': routes}, include_defaults=False)


def effective_model(plan, member):
    return member.get('model') or plan['providers'][member['provider_id']].get('model')


class StudioV2Service(StudioService):
    def _pending_stage_changes(self, job_id):
        job = self._job(job_id)
        plan = _load(job/'pipeline.v2.json', {})
        current = plan.get('revision', 0)
        applied = _load(job/'.team-state.json', {}).get('plan_revision', 0)
        if current <= applied:
            return set()
        history = _load(job/'.plan-revisions.json', {})
        return {stage for entry in history.get('entries', []) if applied < entry.get('revision', 0) <= current
                for stage in entry.get('affected_stages', [])}

    def _stage_rows(self, job_id):
        rows = super()._stage_rows(job_id)
        if any(row['name'] == 'qa' and row['status'] == 'DONE' for row in rows) and 'qa' in self._pending_stage_changes(job_id):
            # A read-only projection lets an explicitly resumed revision reach
            # the engine. The old DB/state/files remain historical evidence.
            rows = [{**row, 'status': 'READY', 'detail': 'A saved goal/assignment revision requires a fresh real QA run.'}
                    if row['name'] == 'qa' else row for row in rows]
        return rows

    def get_plan(self, pipeline_id):
        job = self._job(pipeline_id)
        recover_plan_update(job)
        path = self._inside(job/'pipeline.v2.json')
        if not path.is_file():
            raise ServiceError('This job has no independent V2 plan; create or clone a V2 pipeline first.')
        return normalize_plan(_load(path), pipeline_id=pipeline_id)

    def _started(self, pipeline_id):
        job = self._job(pipeline_id)
        if any((job/name).exists() for name in ('.v2-started.json', '.team-state.json', '.team-budget.json')):
            return True
        return any(row['attempts'] > 0 or row['status'] == 'RUNNING' for row in self._stage_rows(pipeline_id))

    def _write_plan(self, job, plan):
        self._atomic(job/'pipeline.v2.json', plan)
        task = _load(job/'task.json', {})
        task.update(job_id=plan['pipeline_id'], kind=plan['kind'], goal=plan['goal'], budget_usd=plan['total_budget_usd'],task_epoch=plan.get('task_epoch',0))
        self._atomic(job/'task.json', task)
        self._atomic(job/'.workers.json', plan_registry(plan).to_config())
        servers = {entry['id']: {'name': entry.get('name', entry['id']), 'enabled': entry.get('enabled', True),
            'roles': entry.get('roles', list(ROLES)), **entry['config']} for entry in plan['mcp']}
        self._atomic(job/'mcp.v2.json', {'mcpServers': servers})

    def create_pipeline(self, data):
        plan = normalize_plan(data, self.get_settings())
        pipeline_id = plan['pipeline_id']
        with self._lock_for(pipeline_id):
            from studio_pipeline_delete import reject_deleted_id
            reject_deleted_id(self.root,pipeline_id)
            # Call the base creator directly; it writes only this new job and
            # creates no model process. A zero-cost subscription plan is valid.
            StudioService.init_job(self, pipeline_id, goal=plan['goal'], task_patch={'kind': plan['kind']})
            self._write_plan(self._job(pipeline_id), plan)
        return self.get_state(pipeline_id)['jobs'][0]

    def init_job(self, job_id, *, goal=None, budget_usd=None, task_patch=None):
        patch = task_patch or {}
        return self.create_pipeline({'pipeline_id': job_id, 'kind': patch.get('kind', 'software'),
            'goal': goal or patch.get('goal', 'Create a useful isolated project'),
            'total_budget_usd': self.get_settings()['policy']['job_budget_usd'] if budget_usd is None else budget_usd})

    create_job = init_job

    def update_pipeline(self, pipeline_id, data):
        with self._lock_for(pipeline_id):
            if not isinstance(data, dict):
                raise ServiceError('Pipeline update must be a plan object or patch.')
            if 'pipeline_id' in data and data['pipeline_id'] != pipeline_id:
                raise ServiceError('An update cannot rename or overwrite another pipeline.')
            job = self._job(pipeline_id)
            try:
                with pipeline_edit_lease(job):
                    recover_plan_update(job, lease_held=True)
                    original = normalize_plan(_load(job/'pipeline.v2.json'), pipeline_id=pipeline_id)
                    mode = self._edit_mode(pipeline_id, lease_held=True)
                    if mode == 'locked':
                        raise ServiceError('Pause this pipeline and wait for its owner to exit before editing; completed plans can be cloned.')
                    if 'task_epoch' in data and data['task_epoch'] != original.get('task_epoch',0):raise ServiceError('Task epoch is host-owned.')
                    if mode == 'cancelled':
                        from studio_task_cycles import finish_new_task
                        marker=_load(job/'cancelled.json',{})
                        epoch=marker.get('task_epoch',0)+1
                        history=_load(job/'.plan-revisions.json',{}).get('entries',[])
                        if original.get('task_epoch',0)==epoch and any(e.get('task_replaced') is True and e.get('task_epoch')==epoch for e in history):
                            finish_new_task(self,job,new_epoch=epoch)
                        else:
                            self._paused_patch(original,data)
                            plan=normalize_plan({**original,**copy.deepcopy(data)})
                            plan['task_epoch']=epoch
                            self._commit_paused(job,original,plan,source='new-task')
                            finish_new_task(self,job,new_epoch=epoch)
                    elif mode in ('paused','ready'):
                        self._paused_patch(original, data)
                        self._commit_paused(job, original, normalize_plan({**original, **copy.deepcopy(data)}))
                    else:
                        if 'revision' in data and data['revision'] != original['revision']:
                            raise ServiceError('This plan revision is stale; reload before editing.')
                        plan = normalize_plan({**original, **copy.deepcopy(data), 'pipeline_id': pipeline_id})
                        plan['revision'] = original['revision']
                        self._write_plan(job, plan)
            except (RuntimeError, ValueError) as error:
                raise ServiceError(str(error)) from None
        return self.get_state(pipeline_id)['jobs'][0]

    def _edit_mode(self, pipeline_id, *, lease_held=False):
        job = self._job(pipeline_id)
        if not lease_held:
            try:
                with pipeline_edit_lease(job):
                    return self._edit_mode(pipeline_id, lease_held=True)
            except RuntimeError:
                return 'locked'
        if not self._idle_for_edit(pipeline_id):
            return 'locked'
        if _load(job/'cancelled.json',{}).get('status')=='CANCELLED':return 'cancelled'
        from studio_task_cycles import pending_task_replacement
        if pending_task_replacement(job,_load(job/'pipeline.v2.json',{}),_load(job/'.team-state.json',{})):return 'ready'
        if not self._started(pipeline_id):
            return 'full'
        team = _load(job/'.team-state.json', {})
        checkpoint = _load(job/'checkpoint'/'metadata.json', {})
        if team.get('status') == 'PAUSED' or checkpoint.get('state') == 'PAUSED':
            return 'paused'
        return 'locked'

    def _idle_for_edit(self, pipeline_id):
        job = self._job(pipeline_id)
        with self._guard:
            thread = self._threads.get(pipeline_id)
            runtime = copy.deepcopy(self._runtime.get(pipeline_id, {}))
        if thread and thread.is_alive():
            return False
        getter = getattr(self.transport, 'get_job_runtime', None) if self.transport else None
        if callable(getter):
            runtime.update(getter(pipeline_id) or {})
        if runtime.get('running') or str(runtime.get('status', '')).upper() in ('RUNNING', 'PAUSING'):
            return False
        team = _load(job/'.team-state.json', {})
        checkpoint = _load(job/'checkpoint'/'metadata.json', {})
        if team.get('status') == 'PAUSING' or checkpoint.get('state') == 'PAUSING':
            return False
        return True

    def _can_decide_budget(self, job_id, *, lease_held=False):
        job = self._job(job_id)
        if not lease_held:
            try:
                with pipeline_edit_lease(job):
                    return self._can_decide_budget(job_id, lease_held=True)
            except RuntimeError:
                return False
        if not self._idle_for_edit(job_id):
            return False
        if self._edit_mode(job_id, lease_held=True) == 'paused':
            return True
        team = _load(job/'.team-state.json', {})
        return team.get('status') == 'WAITING_BUDGET' or any(member.get('status') == 'WAITING_BUDGET'
            for member in team.get('members', {}).values())

    def _paused_patch(self, original, data):
        if 'revision' not in data or isinstance(data['revision'], bool) or not isinstance(data['revision'], int) or data['revision'] != original['revision']:
            raise ServiceError('Supply the current plan revision; a stale paused edit cannot overwrite another edit.')
        immutable = PLAN_FIELDS-{'goal', 'total_budget_usd', 'roles', 'revision'}
        if any(key in data and data[key] != original.get(key) for key in immutable):
            raise ServiceError('Paused edits cannot change providers, models, MCP, policy, kind or pipeline identity.')
        if 'roles' not in data:
            return
        roles = data['roles']
        if not isinstance(roles, dict) or set(roles) != set(original['roles']):
            raise ServiceError('Paused edits must preserve every existing role and member.')
        for role, current in original['roles'].items():
            changed = roles[role]
            if not isinstance(changed, dict) or set(changed)-{'budget_usd','members'}:
                raise ServiceError('Unknown paused role edit field.')
            members = changed.get('members')
            if not isinstance(members, list) or len(members) != len(current['members']):
                raise ServiceError('Members cannot be added or removed while paused; clone to change the team.')
            for before, after in zip(current['members'], members):
                if not isinstance(after, dict) or set(after)-set(before):
                    raise ServiceError('Unknown paused member edit field.')
                if any(after.get(key) != before[key] for key in ('id','provider_id','model','required')):
                    raise ServiceError('Paused edits cannot replace a member, provider, model or required flag.')

    def _plan_targets(self, job, plan):
        task = _load(job/'task.json', {})
        task.update(job_id=plan['pipeline_id'], kind=plan['kind'], goal=plan['goal'], budget_usd=plan['total_budget_usd'],task_epoch=plan.get('task_epoch',0))
        servers = {entry['id']: {'name': entry.get('name', entry['id']), 'enabled': entry.get('enabled', True),
            'roles': entry.get('roles', list(ROLES)), **entry['config']} for entry in plan['mcp']}
        return {'pipeline.v2.json': plan, 'task.json': task, '.workers.json': plan_registry(plan).to_config(),
                'mcp.v2.json': {'mcpServers': servers}}

    def _commit_paused(self, job, original, plan, *, requests=None, source='paused-edit'):
        if plan == original:
            return False
        plan['revision'] = original['revision']+1
        targets = self._plan_targets(job, plan)
        old_roles, old_members = budget_caps(original)
        new_roles, new_members = budget_caps(plan)
        if (job/'.team-budget.json').exists():
            ledger = TeamBudgetLedger(job/'.team-budget.json', original['total_budget_usd'], old_roles, old_members)
            targets['.team-budget.json'] = ledger.reconfigure(plan['total_budget_usd'], new_roles, new_members, persist=False)
        changed_fields = []
        affected = set()
        affected_members = []
        if plan['goal'] != original['goal']:
            changed_fields.append('goal')
            affected.update(('gpt_base','claude_vfx','gpt_integrate','qa'))
            affected_members.extend(member['id'] for team in plan['roles'].values() for member in team['members'])
        for role, team in plan['roles'].items():
            if team['budget_usd'] != original['roles'][role]['budget_usd']:
                changed_fields.append('roles.'+role+'.budget_usd')
            for before, after in zip(original['roles'][role]['members'], team['members']):
                if after['budget_usd'] != before['budget_usd']:
                    changed_fields.append('members.'+after['id']+'.budget_usd')
                if after['assignment'] != before['assignment']:
                    changed_fields.append('members.'+after['id']+'.assignment')
                    affected_members.append(after['id'])
                    affected.add(ROLE_STAGES[role])
                    affected.update(('gpt_integrate','qa'))
                    if role == 'research':
                        affected.update(('gpt_base','claude_vfx'))
        replaced=plan.get('task_epoch',0)!=original.get('task_epoch',0)
        if replaced:
            if source!='new-task' or plan['task_epoch']!=original.get('task_epoch',0)+1:raise ServiceError('Invalid host task replacement epoch.')
            changed_fields.append('task_epoch');affected.update(('gpt_base','claude_vfx','research','gpt_integrate','qa'))
            affected_members.extend(member['id'] for team in plan['roles'].values() for member in team['members'])
        if plan['total_budget_usd'] != original['total_budget_usd']:
            changed_fields.append('total_budget_usd')
        revisions = _load(job/'.plan-revisions.json', {'schema': REVISION_SCHEMA, 'pipeline_id': job.name, 'entries': []})
        if revisions.get('schema') != REVISION_SCHEMA or revisions.get('pipeline_id') != job.name or not isinstance(revisions.get('entries'), list):
            raise ServiceError('Plan revision history is invalid and cannot be replaced.')
        revisions['entries'].append({'revision': plan['revision'], 'previous_revision': original['revision'], 'at': self.clock(),
            'source': source, 'changed_fields': changed_fields, 'affected_stages': sorted(affected),
            'affected_members': sorted(set(affected_members)), 'budget_only': not affected, 'previous_plan': original,
            **({'task_replaced':True,'previous_task_epoch':original.get('task_epoch',0),'task_epoch':plan['task_epoch']} if replaced else {})})
        targets['.plan-revisions.json'] = revisions
        requests = copy.deepcopy(requests) if requests is not None else load_budget_requests(job)
        for item in requests['requests']:
            if item['status'] == 'PENDING':
                item.update(status='SUPERSEDED', superseded_at=self.clock(), superseded_by_revision=plan['revision'])
        targets['.budget-requests.json'] = requests
        commit_plan_update(job, targets, lease_held=True)
        return True

    def budget_requests(self, job_id):
        job = self._job(job_id)
        recover_plan_update(job)
        return self._public(load_budget_requests(job))

    def decide_budget_request(self, job_id, request_id, approved, increase_usd=0, expected_revision=None):
        if not isinstance(approved, bool):
            raise ServiceError('A budget decision must explicitly approve or decline.')
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ServiceError('Supply the current integer plan revision for this decision.')
        with self._lock_for(job_id):
            job = self._job(job_id)
            try:
                with pipeline_edit_lease(job):
                    recover_plan_update(job, lease_held=True)
                    plan = normalize_plan(_load(job/'pipeline.v2.json'), pipeline_id=job_id)
                    data = load_budget_requests(job)
                    item = next((item for item in data['requests'] if item['id'] == request_id), None)
                    if item is None:
                        raise ServiceError('This budget request does not exist on this pipeline.')
                    if item['status'] in ('APPROVED','DECLINED'):
                        same = item['status'] == ('APPROVED' if approved else 'DECLINED')
                        if approved:
                            same = same and _amount(increase_usd, 'Budget increase') == item.get('approved_increase_usd')
                        if same and expected_revision == item['plan_revision']:
                            return copy.deepcopy(item)
                        raise ServiceError('A decided budget request cannot be changed or approved twice.')
                    if item['status'] != 'PENDING' or expected_revision != plan['revision'] or item['plan_revision'] != plan['revision']:
                        raise ServiceError('This budget request or plan revision is stale; reload before deciding.')
                    if not self._can_decide_budget(job_id, lease_held=True):
                        raise ServiceError('Wait for the budget-stopped owner to exit, or pause this pipeline before deciding.')
                    if not approved:
                        item.update(status='DECLINED', decided_at=self.clock())
                        commit_plan_update(job, {'.budget-requests.json': data}, lease_held=True)
                        return copy.deepcopy(item)
                    increase = _amount(increase_usd, 'Budget increase', positive=True)
                    candidate = copy.deepcopy(plan)
                    role = item['role']
                    member = next((member for member in candidate['roles'].get(role, {}).get('members', []) if member['id'] == item['member_id']), None)
                    if member is None or candidate['providers'][member['provider_id']]['billing'] != 'metered':
                        raise ServiceError('Only a planned API member can receive a USD budget increase.')
                    old_roles, old_members = budget_caps(plan)
                    role_used = amount(0)
                    if (job/'.team-budget.json').exists():
                        ledger = TeamBudgetLedger(job/'.team-budget.json', plan['total_budget_usd'], old_roles, old_members)
                        snapshot = ledger.snapshot()
                        if snapshot['cost_reconciliation_required']:
                            raise ServiceError('Reconcile unknown or interrupted costs before approving a budget increase.')
                        if amount(snapshot['remaining_usd']) < amount(increase):
                            raise ServiceError('The remaining total budget is smaller than this increase; edit the pipeline total first.')
                        role_stats = snapshot['roles'][role]
                        role_used = amount(role_stats['spent_usd'])+amount(role_stats['reserved_usd'])
                    member['budget_usd'] = float(amount(member['budget_usd'])+amount(increase))
                    # Increase a depleted shared role by enough to fund this
                    # grant; existing role headroom need not inflate the total.
                    old_role_cap = amount(candidate['roles'][role]['budget_usd'])
                    candidate['roles'][role]['budget_usd'] = float(max(old_role_cap, amount(member['budget_usd']), role_used+amount(increase)))
                    if candidate['roles'][role]['budget_usd'] > candidate['total_budget_usd']:
                        raise ServiceError('The requested member/role cap exceeds this pipeline total; edit the total budget first.')
                    item.update(status='APPROVED', decided_at=self.clock(), approved_increase_usd=increase)
                    self._commit_paused(job, plan, normalize_plan(candidate), requests=data, source='budget-request:'+request_id)
                    return copy.deepcopy(item)
            except (RuntimeError, ValueError) as error:
                raise ServiceError(str(error)) from None

    def clone_pipeline(self, source_id, new_id):
        plan = self.get_plan(source_id)
        plan['pipeline_id'] = _identifier(new_id, 'New pipeline ID')
        plan['revision'] = 0
        plan['task_epoch'] = 0
        return self.create_pipeline(plan)

    def _request(self, action, request, approved_paid, provider_budgets, paid_approval):
        request = copy.deepcopy(request or {})
        if 'plan' not in request:
            return super()._request(action, request, approved_paid, provider_budgets, paid_approval)
        plan = self.get_plan(request['plan']['pipeline_id'])
        if request['plan'] != plan:
            raise ServiceError('A running request cannot replace the saved frozen plan.')
        _validate_no_secrets(request)
        approval = copy.deepcopy(paid_approval if paid_approval is not None else request.get('paid_approval', {}))
        if not isinstance(approval, dict) or not isinstance(approval.get('approved', approved_paid), bool):
            raise ServiceError('API approval must be explicit true or false.')
        approved = approval.get('approved', approved_paid)
        providers = approval.get('providers', []) if approved else []
        if not isinstance(providers, list) or not all(isinstance(name, str) and name in plan['providers'] for name in providers):
            raise ServiceError('Approval must name only providers frozen in this pipeline.')
        all_members = {member['id']: member for team in plan['roles'].values() for member in team['members']}
        member_selection = approval.get('members', approval.get('member_ids'))
        if member_selection is None:
            member_selection = [member['id'] for member in all_members.values() if member['provider_id'] in providers]
        if not isinstance(member_selection, list) or not all(isinstance(name, str) and name in all_members for name in member_selection):
            raise ServiceError('Approval must name only members in this frozen pipeline.')
        allowed_members = [name for name in dict.fromkeys(member_selection) if approved and
            all_members[name]['provider_id'] in providers and plan['providers'][all_members[name]['provider_id']]['billing'] == 'metered']
        if approved and any(plan['providers'][all_members[name]['provider_id']]['billing'] == 'metered' and
                            all_members[name]['provider_id'] not in providers for name in member_selection):
            raise ServiceError('A paid member needs its own frozen provider approval too.')
        cap = _amount(approval.get('job_budget_usd', plan['total_budget_usd']), 'Approved cumulative pipeline budget')
        if cap > plan['total_budget_usd'] or allowed_members and cap <= 0:
            raise ServiceError('The approved cap must fit this pipeline total and be positive for paid members.')
        connection = _amount(approval.get('connection_budget_usd', plan['policy']['connection_budget_usd']), 'Connection threshold', positive=True)
        if connection > .2:
            raise ServiceError('Connection tests remain capped at 0.20 USD per approved probe.')
        selected_providers = list(dict.fromkeys(all_members[name]['provider_id'] for name in allowed_members))
        request.update(action=action, plan=plan, approved_paid=bool(allowed_members), allowed_metered_providers=selected_providers,
            allowed_metered_members=allowed_members, approved_cumulative_budget_usd=cap,
            provider_budgets={}, policy=plan['policy'],
            paid_approval={'approved': bool(allowed_members), 'providers': selected_providers, 'members': allowed_members,
                           'job_budget_usd': cap, 'connection_budget_usd': connection})
        return request

    def _start(self, job_id, action, *, request=None, approved_paid=False, provider_budgets=None, paid_approval=None):
        plan = self.get_plan(job_id)
        request = copy.deepcopy(request or {})
        if 'plan' in request and request['plan'] != plan:
            raise ServiceError('An execution request cannot override its saved pipeline plan.')
        request['plan'] = plan
        authorized = self._request(action, request, approved_paid, provider_budgets, paid_approval)
        with self._lock_for(job_id):
            if self.transport is None or not callable(getattr(self.transport, 'run_job', None)):
                raise ServiceError('The V2 application runner transport is not configured.')
            rows = self._stage_rows(job_id)
            if not any(row['name'] == 'qa' and row['status'] == 'DONE' for row in rows):
                self._atomic(self._job(job_id)/'.v2-started.json', {'pipeline_id': job_id, 'started_at': self.clock(),
                    'plan_sha256': hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()})
            return super()._start(job_id, action, request=authorized, approved_paid=authorized['approved_paid'],
                                  provider_budgets={}, paid_approval=authorized['paid_approval'])

    def retry_job(self, job_id, stage=None, *, member_id=None, **kwargs):
        plan = self.get_plan(job_id)
        if member_id is None:
            return super().retry_job(job_id, stage, **kwargs)
        members = {member['id']: (role, member) for role, team in plan['roles'].items() for member in team['members']}
        if member_id not in members:
            raise ServiceError('Retry member is not part of this pipeline.')
        team = _load(self._job(job_id)/'.team-state.json', {})
        status = team.get('members', {}).get(member_id, {}).get('status')
        role = members[member_id][0]
        curator = team.get('stages', {}).get(ROLE_STAGES[role], {})
        curator_failed = curator.get('lead_id') == member_id and curator.get('curation_status') in ('FAILED', 'BLOCKED', 'INTERRUPTED')
        if status not in ('FAILED', 'BLOCKED', 'INTERRUPTED', 'BUDGET_STOP', 'WAITING_CONNECTION', 'WAITING_AUTHORIZATION', 'WAITING_APPROVAL') and not curator_failed:
            raise ServiceError('Only a failed, blocked or waiting member/curator can be explicitly retried.')
        if stage is not None and stage != ROLE_STAGES[role]:
            raise ServiceError('Retry stage does not match this member role.')
        request = copy.deepcopy(kwargs.pop('request', None) or {})
        request.update(member_id=member_id, stage=ROLE_STAGES[role])
        return self._start(job_id, 'retry', request=request, **kwargs)

    retry = retry_job

    def get_state(self, job_id=None):
        state = super().get_state(job_id)
        for row in state['jobs']:
            job = self._job(row['job_id'])
            if not (job/'pipeline.v2.json').is_file():
                row.update(editable=False, plan_frozen=False, plan=None, team=None)
                continue
            plan = self.get_plan(row['job_id'])
            mode = self._edit_mode(row['job_id'])
            row.update(plan=self._public(plan), edit_mode=mode, editable=mode != 'locked', plan_frozen=self._started(row['job_id']),
                       kind=plan['kind'], goal=self._public(plan['goal']), total_budget_usd=plan['total_budget_usd'])
            if mode == 'paused' and not row.get('running'):
                row['status'] = 'PAUSED'
            row['revalidation_required'] = 'qa' in self._pending_stage_changes(row['job_id'])
            row['budget_requests'] = self.budget_requests(row['job_id'])['requests']
            row['can_decide_budget'] = self._can_decide_budget(row['job_id'])
            try:
                team = _load(self._inside(job/'.team-state.json'), None)
            except (ValueError, OSError):
                team = {'schema': 'ai-studio-team-state/v2', 'pipeline_id': row['job_id'], 'status': 'NEEDS_ATTENTION',
                        'error': 'The saved team state could not be read; no completion is assumed.', 'members': {}, 'stages': {}}
            if not isinstance(team, dict):
                team = {'schema': 'ai-studio-team-state/v2', 'pipeline_id': row['job_id'], 'status': 'READY',
                        'stages': row['stages'], 'members': {}, 'references': {}}
            for role, configured in plan['roles'].items():
                for member in configured['members']:
                    actual = team.setdefault('members', {}).setdefault(member['id'], {})
                    actual.update(id=member['id'], role=role, stage=ROLE_STAGES[role], provider_id=member['provider_id'],
                        model=actual.get('model') or effective_model(plan, member), effective_model=effective_model(plan, member),
                        assignment=member['assignment'], required=member['required'], budget_usd=member['budget_usd'])
                    actual['billing'] = plan['providers'][member['provider_id']]['billing']
                    actual['budget_available'] = actual['billing'] == 'metered'
                    actual.setdefault('status', 'DORMANT' if role == 'research' else 'READY')
                    actual.setdefault('attempts', 0)
                    actual_events = [event for event in state['latest_events'] if event.get('job_id') == row['job_id'] and event.get('member_id') == member['id']]
                    actual['log_tail'] = [event.get('text') or event.get('activity', '') for event in actual_events][-80:]
                    if actual_events:
                        actual['activity'] = actual_events[-1].get('text') or actual_events[-1].get('activity')
                        actual['latest_tool_event'] = next((event.get('text') or event.get('activity') for event in reversed(actual_events)
                            if 'tool' in str(event.get('activity', '')).lower() or event.get('activity') in ('command', 'file')), None)
            rolecaps, membercaps = budget_caps(plan)
            if (job/'.team-budget.json').is_file():
                try:
                    ledger = TeamBudgetLedger(job/'.team-budget.json', plan['total_budget_usd'], rolecaps, membercaps)
                    active = None
                    if row.get('running') or row.get('runtime', {}).get('running'):
                        raw = _load(job/'.team-budget.json', {})
                        active = [key for key, invocation in raw.get('invocations', {}).items() if invocation.get('state') == 'pending']
                    row['budget'] = ledger.snapshot(active_invocations=active)
                except (ValueError, RuntimeError, OSError) as error:
                    row['budget'] = {'total_cap_usd': str(plan['total_budget_usd']), 'spent_usd': None, 'reserved_usd': None,
                        'remaining_usd': None, 'available_usd': '0', 'blocked': True, 'costs_complete': False,
                        'cost_reconciliation_required': True, 'block_reasons': [self._redact(str(error))]}
            else:
                row['budget'] = {'total_cap_usd': str(plan['total_budget_usd']), 'cap_usd': plan['total_budget_usd'],
                    'spent_usd': '0', 'reserved_usd': '0', 'remaining_usd': str(plan['total_budget_usd']),
                    'available_usd': str(plan['total_budget_usd']), 'costs_complete': True, 'blocked': False,
                    'roles': {role: {'cap_usd': item['budget_usd'], 'spent_usd': '0', 'reserved_usd': '0'} for role, item in plan['roles'].items()},
                    'members': {member: {'role': item['role'], 'cap_usd': item['budget_usd'], 'spent_usd': '0', 'reserved_usd': '0'} for member, item in membercaps.items()}}
            team['budget'] = row['budget']
            if isinstance(team.get('stages'), dict):
                row['stages'] = {name: {**row['stages'].get(name, {}), **self._public(stage)} for name, stage in team['stages'].items()}
            if row['revalidation_required'] and 'qa' in row['stages']:
                row['stages']['qa'].update(status='READY', detail='A saved goal/assignment revision requires a fresh real QA run.')
            if mode=='ready' and not row.get('running'):
                row.update(status='READY',can_start=not row['budget'].get('blocked',False),task_replacement_pending=True)
                team.update(status='READY',pending_task_epoch=plan['task_epoch'])
                for member in team['members'].values():member['status']='DORMANT' if member['role']=='research' else 'READY'
                for name,stage in row['stages'].items():stage['status']='READY' if name in ('gpt_base','claude_vfx') else 'DORMANT'
                for name,stage in team.get('stages',{}).items():stage['status']='READY' if name in ('gpt_base','claude_vfx') else 'DORMANT'
            row['team'] = self._public(team)
        state['budgets'] = {row['job_id']: row['budget'] for row in state['jobs']}
        state['budget_requests'] = {row['job_id']: row.get('budget_requests', []) for row in state['jobs']}
        state['app'].update(version='2.5', capabilities={**state['app'].get('capabilities', {}),
            'pipeline_teams': True, 'hierarchical_budget': True, 'frozen_pipeline_plans': True,
            'paused_plan_edit': True, 'budget_requests': True})
        return state
