"""Finite role-based executors. Imported only when workers_file is enabled.

No provider fallback. Native subscription auth and existing paid approval
remain authoritative. A desktop handoff creates files and then exits.
"""
from __future__ import annotations
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
from urllib.parse import urlsplit

from adapter_budget import BudgetLedger
from worker_adapters import ProviderRegistry, adapter_request, decode_completion

STAGE_ROLES = {'gpt_base':'gameplay', 'claude_vfx':'vfx',
               'gpt_integrate':'integration', 'research':'research'}
_LEDGERS = {}
_LEDGER_LOCK = threading.RLock()
_DISPATCH_LOCK = threading.RLock()
_DISPATCH_LOCAL = threading.local()
_DISPATCH_HOOKS = {}


def mark_dispatched():
    """Record a launched owned process or attempted HTTP request for this thread."""
    state=getattr(_DISPATCH_LOCAL,'state',None)
    if state is not None:
        state['dispatched']=True


def terminate_windows_owned_child(process):
    """Stop an already owned child without treating OS cleanup as AI dispatch.

    The dispatch hook blocks new worker launches after STOP. Windows taskkill
    is host maintenance for this existing Popen, so use its original spawn.
    """
    import subprocess
    if os.name!='nt': raise RuntimeError('Windows child cleanup is Windows only.')
    if process.poll() is not None: return
    with _DISPATCH_LOCK:
        spawn=subprocess.Popen
        for hooks in _DISPATCH_HOOKS.values():
            if spawn is hooks['observed_spawn']:
                spawn=hooks['spawn']; break
    taskkill=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'taskkill.exe'
    if not taskkill.is_absolute() or not taskkill.is_file():
        raise OSError('The Windows process cleanup tool is unavailable.')
    cleanup=spawn([str(taskkill),'/PID',str(process.pid),'/T','/F'],
                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                  creationflags=subprocess.CREATE_NO_WINDOW)
    try: cleanup.wait(timeout=15)
    except subprocess.TimeoutExpired:
        cleanup.kill(); cleanup.wait(timeout=5); raise


@contextmanager
def observe_dispatch(p):
    """Observe real execution boundaries without mixing concurrent role accounting."""
    key=(id(p.subprocess),id(p.urllib.request))
    state={'dispatched':False}
    previous=getattr(_DISPATCH_LOCAL,'state',None)
    with _DISPATCH_LOCK:
        hooks=_DISPATCH_HOOKS.get(key)
        if hooks is None:
            spawn=p.subprocess.Popen
            request=p.urllib.request.urlopen
            def observed_spawn(*args,**kwargs):
                from studio_controller_lifecycle import check_dispatch
                check_dispatch()
                if p.STOP.is_set():
                    raise RuntimeError('Paused; no new process is launched.')
                process=spawn(*args,**kwargs)
                mark_dispatched()
                return process
            def observed_request(*args,**kwargs):
                from studio_controller_lifecycle import check_dispatch
                check_dispatch()
                if p.STOP.is_set():
                    raise RuntimeError('Paused; no new API request is sent.')
                mark_dispatched()
                return request(*args,**kwargs)
            hooks={'count':0,'spawn':spawn,'request':request,
                   'observed_spawn':observed_spawn,'observed_request':observed_request}
            _DISPATCH_HOOKS[key]=hooks
            p.subprocess.Popen=observed_spawn
            p.urllib.request.urlopen=observed_request
        hooks['count']+=1
    _DISPATCH_LOCAL.state=state
    try:
        yield state
    finally:
        _DISPATCH_LOCAL.state=previous
        with _DISPATCH_LOCK:
            hooks['count']-=1
            if hooks['count']==0:
                if p.subprocess.Popen is hooks['observed_spawn']:
                    p.subprocess.Popen=hooks['spawn']
                if p.urllib.request.urlopen is hooks['observed_request']:
                    p.urllib.request.urlopen=hooks['request']
                del _DISPATCH_HOOKS[key]


def native_config(p,c,provider,*,allow_default_prompt=False):
    """Resolve the selected native gateway and its own credential before dispatch."""
    if provider.adapter=='kimi':
        from studio_kimi_adapter import check_config
        return check_config(p,c,provider)
    cfg=dict(c)
    expected_ref='current-codex-profile' if provider.adapter=='codex' else 'current-claude-profile'
    settings={}
    configured=c.get('provider_settings',{})
    if not isinstance(configured,dict):
        raise RuntimeError('Native provider settings must be a mapping.')
    if provider.settings_ref not in (None,expected_ref):
        selected=configured.get(provider.settings_ref)
        if not isinstance(selected,dict):
            raise RuntimeError('A different native gateway/profile needs its own configured adapter; an unknown settings reference is not silently substituted.')
        settings.update(selected)
    # Desktop settings are stored by provider ID even when settings_ref is absent.
    if provider.provider_id in configured:
        selected=configured[provider.provider_id]
        if not isinstance(selected,dict):
            raise RuntimeError('Native provider settings must be a mapping.')
        settings.update(selected)
    endpoint=settings.get('endpoint') or settings.get('base_url')
    if provider.adapter=='claude':
        if endpoint:
            cfg['claude_base_url']=endpoint
        if settings.get('auth'):
            cfg['claude_auth']=settings['auth']
        actual=cfg.get('claude_base_url','')
        try:
            if not isinstance(actual,str):
                raise TypeError('Gateway must be text.')
            parsed=urlsplit(actual)
            valid=(parsed.scheme=='https' and bool(parsed.hostname)
                   and parsed.username is None and parsed.password is None)
            parsed.port  # Reject invalid port syntax before any credential is used.
        except (TypeError,ValueError):
            valid=False
        if not valid:
            raise RuntimeError('Only HTTPS Claude gateways without embedded credentials are accepted.')
        if cfg.get('claude_auth') not in ('api_key','bearer'):
            raise RuntimeError('Claude authentication must be api_key or bearer.')
        private=c.get('_provider_secrets',{}).get(provider.provider_id) or p.SECRETS.get('provider:'+provider.provider_id)
        current=(provider.provider_id=='claude-code' or provider.settings_ref=='current-claude-profile')
        current=current and actual.rstrip('/')==str(c.get('claude_base_url','')).rstrip('/') and cfg.get('claude_auth')==c.get('claude_auth')
        if not private and current:
            private=c.get('_provider_secrets',{}).get('claude-code') or p.SECRETS.get('provider:claude-code') or p.SECRETS.get('claude')
            if not private and allow_default_prompt:
                p.need_secret('claude')
                private=p.SECRETS.get('claude')
        if not isinstance(private,str) or not private.strip():
            raise RuntimeError('The selected Claude provider '+provider.provider_id+' needs its own private credential; no request was sent.')
        # An incoming config may have belonged to a different provider.
        cfg['_claude_credential']=private
        # Redaction registration is separate from explicit credential ownership:
        # a reused default key must not become an alias's own key on a later gateway.
        p.SECRETS['native-credential:'+provider.provider_id]=private
    elif endpoint:
        raise RuntimeError('Codex remains a ChatGPT subscription adapter; API endpoints belong to an API adapter.')
    if tuple(provider.command)!=(provider.adapter,):
        cfg[provider.adapter+'_command']=list(provider.command)
    if provider.model:
        cfg['codex_model' if provider.adapter=='codex' else 'claude_model']=provider.model
    return cfg


def private_profile_path(p,c,path):
    """Operational credentials are confined to configured private profiles, outside outputs."""
    profiles=Path(c.get('profiles_dir',str(p.PROFILES)))
    if not profiles.is_absolute():
        raise RuntimeError('Private profile paths must be absolute.')
    p.ensure_plain_path(profiles)
    p.ensure_plain_path(path)
    if (any(part.casefold()=='outputs' for part in profiles.parts)
            or not path.absolute().is_relative_to(profiles.absolute())
            or path.absolute()==profiles.absolute()):
        raise RuntimeError('Operational credentials must stay inside private profiles outside outputs.')
    return profiles


def write_private_json(p,c,path,data):
    """Write exact runtime values; public dump/log paths still use normal redaction."""
    private_profile_path(p,c,path)
    temporary=path.with_name(path.name+'.tmp')
    private_profile_path(p,c,temporary)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    os.replace(temporary,path)


def registry_path(p, c):
    path = Path(c.get('workers_file', str(p.ROOT/'workers.local.json')))
    if not path.is_absolute():
        path = p.ROOT/path
    p.ensure_plain_path(path)
    return path


def read_registry(p, c, job=None):
    snapshot = job/'.workers.json' if job else None
    source = snapshot if snapshot and snapshot.is_file() else registry_path(p,c)
    p.ensure_plain_path(source)
    data = p.load(source)
    return ProviderRegistry.from_config(data,include_defaults=False), data


def freeze_registry(p,c,job):
    target=job/'.workers.json'
    if not target.exists():
        registry,data=read_registry(p,c)
        p.dump(target,registry.to_config())


def provider_for(p,c,job,stage):
    registry,_=read_registry(p,c,job)
    role=STAGE_ROLES[stage]
    selected=None
    choice=job/'references'/'provider_choice.json'
    if role=='research' and choice.is_file():
        selected=p.load(choice).get('provider_id')
    return registry.resolve(role,provider_id=selected,require_ready=True)


def preflight(p,c,job,stages):
    actionable=[stage for stage in stages if stage in STAGE_ROLES]
    if not actionable:
        return
    freeze_registry(p,c,job)
    for stage in actionable:
        provider=provider_for(p,c,job,stage)
        if provider.billing=='metered':
            p.require_paid_approval()
        if provider.adapter=='codex':
            p.check_auth(native_config(p,c,provider))
        elif provider.adapter in ('claude','kimi'):
            native_config(p,c,provider,allow_default_prompt=True)
        elif provider.adapter=='openrouter-research':
            p.need_secret('research')


def prepare_native(p,c,role,provider):
    """Give each native invocation a profile containing only its role's MCP."""
    if provider.adapter not in ('claude','codex','kimi') or role not in STAGE_ROLES.values():
        raise RuntimeError('A native profile requires one registered native adapter and role.')
    cfg=native_config(p,c,provider)
    profiles=p.profiles_for(c)
    home=profiles/(provider.adapter+'-'+role)
    private_profile_path(p,c,home)
    home.mkdir(parents=True,exist_ok=True)
    if c.get('_task_kind')=='software':
        tools={}
    elif role=='vfx':
        native=bool(c.get('blender_mcp_command'))
        command=p.command_for('blender_mcp' if native else 'uvx',c)
        args=c.get('blender_mcp_args',[] if native else ['--python','3.11','mcp-for-blender'])
        server={'command':command[0],'args':command[1:]+args,
                'env':{'BLENDER_HOST':'127.0.0.1','BLENDER_PORT':str(c.get('blender_port',9876)),
                       'BLENDER_MCP_SAFE_MODE':'1','BLENDER_MCP_DISABLE_TELEMETRY':'1'}}
        tools={'blender':server}
    elif role in ('gameplay','integration'):
        server={'command':p.executable('node')[0],
                'args':p.godot_launch_args(c,Path(c['godot_mcp_script'])),
                'env':{'GODOT_PATH':c['godot_path'],'GODOT_MCP_STRICT':'true'}}
        tools={'godot':server}
    else:
        tools={}
    from studio_mcp import role_servers, append_codex_server
    builtin_names=set(tools)
    tools=role_servers(p,c,role,tools)
    # Environment references may come from the inherited private environment.
    # Register only substituted values, never harmless literal safety settings.
    mcp_path=Path(c.get('mcp_file',p.ROOT/'mcp.local.json'))
    if mcp_path.is_file():
        for name,entry in p.load(mcp_path).get('mcpServers',{}).items():
            if name not in tools or name in builtin_names:
                continue
            configured=entry.get('config',entry)
            for field in ('headers','env'):
                for variable,reference in (configured.get(field) or {}).items():
                    if isinstance(reference,str) and re.fullmatch(r'\$\{[A-Za-z_][A-Za-z0-9_]*\}',reference):
                        value=tools[name].get(field,{}).get(variable)
                        if value:
                            p.SECRETS['mcp:'+name+':'+field+':'+variable]=value
    if provider.adapter=='kimi':
        from studio_kimi_adapter import prepare_profile
        cwd=Path(c.get('_kimi_workspace',p.ROOT))
        cfg=prepare_profile(p,c,provider,role,home,tools,cwd)
    elif provider.adapter=='claude':
        config_path=home/'mcp.json'
        write_private_json(p,c,config_path,{'mcpServers':tools})
        cfg.update(_claude_role_home=str(home),_claude_role_mcp=str(config_path),
                   _claude_tool_role='godot' if role in ('gameplay','integration') else 'blender')
        cfg['_claude_extra_mcp_tools']=['mcp__'+name+'__*' for name in tools if name not in ('godot','blender')]
    else:
        q=lambda value: json.dumps(str(value),ensure_ascii=False)
        text=('model_provider = "openai"\nforced_login_method = "chatgpt"\n'
              'sandbox_mode = "workspace-write"\napproval_policy = "on-request"\n')
        if cfg.get('codex_model'):
            text+='model = '+q(cfg['codex_model'])+'\n'
        if p.codex_transport_args(cfg):
            text+='enable_request_compression = false\n'
        text+='\n[features]\nplugins = false\nremote_plugin = false\napps = false\nhooks = false\nmulti_agent = false\nunbounded_connection_retries = false\n'
        text+='\n[windows]\nsandbox = "unelevated"\n'
        for name,tool in tools.items():
            text+=append_codex_server(name,tool,q)
        config_path=home/'config.toml'
        private_profile_path(p,c,config_path)
        config_path.write_text(text,encoding='utf-8')
        auth=profiles/'codex'/'auth.json'
        target=home/'auth.json'
        private_profile_path(p,c,auth); private_profile_path(p,c,target)
        if not auth.is_file():
            raise RuntimeError('Reuse the existing ChatGPT login in the private Codex profile first.')
        shutil.copyfile(auth,target)
        cfg['_codex_role_home']=str(home)
    return cfg


def ledger_for(p,c,job):
    if c.get('_v2_budget_view') is not None:
        return c['_v2_budget_view']
    key=str(job.resolve())
    cap=c.get('worker_budget_usd_per_job',c.get('claude_budget_usd_per_job',3))
    with _LEDGER_LOCK:
        if key not in _LEDGERS:
            path=job/'.adapter-state'/'budget.json'
            p.ensure_plain_path(path)
            ledger=BudgetLedger(path,p.cost_decimal(cap,'Shared job budget'))
            # Switching roles/providers never resets existing native Claude spend.
            for workspace in ('art','game','references'):
                for usage in (job/workspace/'.pipeline').glob('*.usage.json'):
                    data=p.load(usage)
                    if data.get('billing_mode')=='subscription':continue
                    if data.get('is_smoke'):
                        continue
                    amount=data.get('reported_total_cost_usd')
                    ledger.import_cost('native:'+str(usage.relative_to(job)),None if amount is None else p.cost_decimal(amount,'Recorded worker cost'))
                for log in (job/workspace/'.pipeline').glob('claude_vfx-*.log'):
                    if not re.fullmatch(r'claude_vfx-[0-9]+\.log',log.name):
                        continue
                    usage=log.with_suffix('.usage.json')
                    if usage.is_file():
                        continue
                    try:
                        outer,_=p.parse_claude_stream(log.read_text(encoding='utf-8',errors='replace'))
                        amount=outer.get('total_cost_usd')
                    except RuntimeError:
                        amount=None
                    ledger.import_cost('native:'+str(usage.relative_to(job)),None if amount is None else p.cost_decimal(amount,'Historic failed worker cost'))
            _LEDGERS[key]=ledger
        return _LEDGERS[key]


def invocation_cap(p,c,job):
    if '_v2_call_cap' in c:
        return p.cost_decimal(c['_v2_call_cap'],'Team member invocation cap')
    registry,_=read_registry(p,c,job)
    count=sum(registry.resolve(role).billing=='metered' for role in ('gameplay','vfx'))
    total=Decimal(str(c.get('worker_budget_usd_per_job',c.get('claude_budget_usd_per_job',3))))
    per_call=Decimal(str(c.get('worker_budget_usd_per_call',c.get('claude_budget_usd_per_call',3))))
    return min(per_call,total/max(1,count))


def task_prompt(p,job,stage,attempt):
    if stage=='research':
        request=p.load(job/'references'/'request.json')
        return ('Find relevant, sourced references for this request. Produce references.json in this workspace. '
                'Use actual search. Distinguish inspected video contents from URLs; never invent inspection or licensing. '
                'Do not modify game or art. End after one bounded answer.\nQUERY DATA:\n'+request['query'])
    prompt=p.base_prompt(job,stage)
    if stage=='claude_vfx' and attempt>1:
        prompt+=('\nRESUME: inspect saved checkpoints and references.json, then continue the supplied task. '
                 'Preserve valid artifacts; do not regenerate completed work.\n')
        if p.load(job/'task.json').get('kind')!='software':
            prompt+='Verify the active Blender file belongs to this art workspace before editing or saving. Do not assume frames exist.\n'
    if stage=='gpt_integrate' and attempt>1:
        for filename in ('godot_import.log','godot_acceptance.log'):
            path=job/'delivery'/filename
            if path.is_file():
                prompt+='\nPREVIOUS QA LOG (fix the project, not the test):\n'+path.read_text(encoding='utf-8')[-10000:]
    return prompt


def exchange_folder(job,stage):
    return job/'.adapter-state'/'tasks'/stage


def external_ready(job,stage):
    return (exchange_folder(job,stage)/'completion.json').is_file()


def external_env(p,c,provider):
    env=p.env_for('none',c)
    settings=c.get('worker_settings',{}).get(provider.settings_ref,{})
    name=settings.get('credential_env') if isinstance(settings,dict) else None
    if name:
        if not isinstance(name,str) or not name.isidentifier() or not name.endswith(('_API_KEY','_AUTH_TOKEN')):
            raise RuntimeError('External credentials must reference one explicit API-key/token environment name.')
        import os
        value=os.environ.get(name)
        if not value:
            raise RuntimeError('The selected provider credential is missing; enter it privately before execution.')
        env[name]=value
        p.SECRETS['worker:'+provider.provider_id]=value
    return env


def worker(p,c,job,stage,attempt):
    registry,_=read_registry(p,c,job)
    role=STAGE_ROLES[stage]
    provider=provider_for(p,c,job,stage)
    cwd=job/('art' if role=='vfx' else 'references' if role=='research' else 'game')
    if role=='integration':
        p.publish_bundle(job)
    prompt=task_prompt(p,job,stage,attempt)+c.get('_v2_prompt_extra','')
    from studio_tool_contracts import append_contract
    prompt=append_contract(prompt,role=role,task_kind=c.get('_task_kind',p.load(job/'task.json').get('kind','fan-wave-demo')),job=job,config=c)
    tag=stage+'-'+str(attempt)
    task_id='task-'+hashlib.sha256((str(job.resolve())+':'+stage).encode()).hexdigest()[:24]
    packet=exchange_folder(job,stage)
    saved=packet/'task.json'
    exchange=None
    if provider.adapter=='manual-desktop' and saved.is_file():
        old=p.load(saved)
        if old.get('provider_id')==provider.provider_id and old.get('prompt')==prompt:
            exchange=old.get('exchange_id')
    extra=('blender-mcp',) if role=='vfx' and provider.adapter in ('codex','claude','kimi') else ('godot-mcp',) if role in ('gameplay','integration') and provider.adapter in ('codex','claude','kimi') else ()
    if c.get('_task_kind')=='software': extra=()
    plan=registry.plan_task(task_id,role,prompt,provider_id=provider.provider_id,
                           inputs={'workspace':str(cwd),'job_spec':p.load(job/'task.json'),
                                   'completion_file':str(packet/'completion.json')},
                           required_capabilities=extra,exchange_id=exchange)
    if plan.status!='planned':
        raise RuntimeError('Selected worker requires setup: '+'; '.join(plan.required_setup))
    if provider.adapter=='manual-desktop':
        p.dump(saved,plan.task.to_dict())
        p.dump(packet/'completion.schema.json',adapter_request(plan)['output_schema'])
        completion=packet/'completion.json'
        if not completion.is_file():
            return {'status':'waiting_external','summary':'Task packet ready: '+str(saved)+'. No model is running; submit its matching completion.json, then run again.','resource_query':'','files':[]}
        decoded=decode_completion(plan,p.load(completion))
        result=decoded['result']
    else:
        reservation=None
        ledger=None
        cost=None
        if provider.billing=='metered':
            p.require_paid_approval()
        # Every local dependency/credential/configuration is checked before any
        # money is reserved. A missing CLI/MCP variable is a connection wait,
        # not an unproven paid invocation that stops independent roles.
        cfg=None
        command=None
        env=None
        payload=None
        if provider.adapter in ('codex','claude','kimi'):
            if provider.adapter=='kimi':c={**c,'_kimi_workspace':str(cwd)}
            cfg=(native_config(p,c,provider) if role=='research' and provider.adapter=='codex'
                 else prepare_native(p,c,role,provider))
            if provider.adapter!='kimi':
                p.command_for(provider.adapter,cfg)
                p.env_for(provider.adapter,cfg)
        elif provider.adapter=='openrouter-research':
            if role!='research':
                raise RuntimeError('This adapter is a reference researcher, not a tool-executing main worker.')
            if not provider.model or not p.SECRETS.get('research'):
                raise RuntimeError('Research key or exact research model ID is missing; no request was sent.')
            cfg={**c,'research_mode':'openrouter','research_model':provider.model}
        else:
            command=list(provider.command)
            first=Path(command[0])
            if not first.is_absolute() or not first.is_file() or first.suffix.lower() in ('.cmd','.bat','.ps1'):
                raise RuntimeError('External adapter must start with an existing absolute native executable; no shell command.')
            env=external_env(p,c,provider)
            payload=json.loads(adapter_request(plan)['stdin'])
        timeout=int(c.get('worker_timeout_seconds',1200))
        if timeout<=0:
            raise RuntimeError('Worker timeout must be positive; no request was sent.')
        if p.STOP.is_set():
            raise RuntimeError('Paused; no new worker is dispatched.')
        from studio_invocation_provenance import record as record_invocation_identity
        record_invocation_identity(p,c,cwd,tag,provider)
        if provider.billing=='metered':
            ledger=ledger_for(p,c,job)
            reservation=('native:'+str((cwd/'.pipeline'/(tag+'.usage.json')).relative_to(job))
                         if provider.adapter=='claude' else 'external:'+task_id+':'+tag)
            allocated=ledger.reserve(reservation,provider.provider_id,invocation_cap(p,c,job))
        else:
            allocated=None
        dispatch={'dispatched':False}
        try:
            with observe_dispatch(p) as dispatch:
                if provider.adapter=='codex':
                    if role=='research':
                        refs=p.codex_research(cfg,job,p.load(job/'references'/'request.json')['query'])
                        p.dump(cwd/'references.json',refs)
                        result={'status':'done','summary':'Sourced research delivered.','resource_query':'','files':['references.json']}
                    else:
                        result=p.codex_call(cfg,cwd,prompt,tag)
                    result=decode_completion(plan,result)['result']
                elif provider.adapter=='claude':
                    cfg['claude_budget_usd_per_call']=float(allocated)
                    result=p.claude_call(cfg,cwd,prompt,tag,blender=role=='vfx')
                    # A returned native result is also dispatch evidence for an
                    # injected/custom caller that does not use our process API.
                    dispatch['dispatched']=True
                    result=decode_completion(plan,result)['result']
                elif provider.adapter=='kimi':
                    from studio_kimi_adapter import call
                    completion=call(p,cfg,cwd,prompt,tag,plan)
                    dispatch['dispatched']=True
                    result=completion['result']
                elif provider.adapter=='openrouter-research':
                    refs=p.research_request(cfg,job,p.load(job/'references'/'request.json')['query'])
                    cost=refs.get('usage',{}).get('cost')
                    if refs.get('source')=='user_provided':
                        cost=Decimal('0')
                    else:
                        dispatch['dispatched']=True
                    p.dump(cwd/'references.json',refs)
                    result={'status':'done','summary':'Sourced research delivered.','resource_query':'','files':['references.json']}
                else:
                    payload['inputs']['budget_usd']=float(allocated) if allocated is not None else None
                    rc,out,err=p.process(command,cwd,env,prompt=json.dumps(payload,ensure_ascii=False)+'\n',
                        timeout=timeout,log=cwd/'.pipeline'/(tag+'.log'))
                    dispatch['dispatched']=True
                    try:
                        raw=json.loads(out)
                        cost=raw.get('usage',{}).get('reported_cost_usd')
                    except (ValueError,AttributeError):
                        raw=None
                    if rc:
                        raise RuntimeError('External worker failed; no provider fallback or automatic retry.')
                    decoded=decode_completion(plan,raw)
                    result=decoded['result']
                    p.dump(cwd/'.pipeline'/(tag+'.completion.json'),raw)
        finally:
            if ledger is not None:
                if provider.adapter=='claude':
                    usage=cwd/'.pipeline'/(tag+'.usage.json')
                    if usage.is_file():
                        cost=p.load(usage).get('reported_total_cost_usd')
                if cost is None and not dispatch['dispatched']:
                    # No owned process was created and no HTTP request entered.
                    # This is positive local evidence, not a guessed interrupted cost.
                    cost=Decimal('0')
                    if provider.adapter=='claude' and usage.is_file():
                        recorded=p.load(usage)
                        recorded.update(reported_total_cost_usd=0.0,dispatch_state='not_dispatched',
                                        cost_evidence='No owned process launch or HTTP dispatch occurred.')
                        p.dump(usage,recorded)
                try:
                    actual=None if cost is None else p.cost_decimal(cost,'Reported worker cost')
                except RuntimeError:
                    ledger.reconcile(reservation,None)
                    raise RuntimeError('The paid worker returned invalid cost data; further paid calls are blocked.') from None
                ledger.reconcile(reservation,actual)
                if cost is None:
                    raise RuntimeError('The paid worker cost is unknown; its outputs are preserved, but further automatic model work is stopped for cost reconciliation.')
    p.validate_result(result)
    if role=='vfx' and result['status']=='done':
        from studio_tool_contracts import reject_hidden_deliverables
        reject_hidden_deliverables(result['files'])
    for relative in result['files']:
        path=p.within(cwd,relative)
        if not path.is_file():
            raise RuntimeError('Selected worker returned a missing artifact: '+relative)
    if result['status']=='done' and role=='vfx' and c.get('_v2_validate_candidate',True):
        p.validate_bundle(job)
    if result['status']=='done' and role=='research':
        refs=cwd/'references.json'
        if not refs.is_file():
            raise RuntimeError('A researcher must deliver references.json, not just claim completion.')
        p.dump(job/'art'/'references.json',p.load(refs))
    return result


def manage(p,c,command,item,value=None,job_id=None):
    job=p.job_path(job_id) if job_id else None
    registry,data=read_registry(p,c,job)
    target=job/'.workers.json' if job else registry_path(p,c)
    if job and any(row['status']=='RUNNING' for row in p.stage_rows(job_id).values()):
        raise RuntimeError('Worker selection cannot change during an active invocation.')
    if command=='worker-list':
        print(json.dumps(registry.to_config(),ensure_ascii=False,indent=2)); return
    if command=='worker-plan':
        plan_job=p.job_path(item)
        for stage in STAGE_ROLES:
            provider=provider_for(p,c,plan_job,stage)
            print(f'{stage}: {provider.provider_id} ({provider.adapter}, {provider.billing})')
        return
    if command=='worker-register':
        if not value:
            raise RuntimeError('Provide a JSON file with one provider definition.')
        definition=Path(value)
        data=registry.to_config()
        data['providers'][item]=p.load(definition)
        updated=ProviderRegistry.from_config(data,include_defaults=False)
    elif command=='worker-remove':
        if any(item in route for route in registry.role_routes.values()):
            raise RuntimeError('Select a replacement for the bound role before removing this provider; no automatic fallback.')
        data=registry.to_config()
        if item not in data['providers']:
            raise RuntimeError('This provider is not registered.')
        del data['providers'][item]
        updated=ProviderRegistry.from_config(data,include_defaults=False)
    else:
        if not value:
            raise RuntimeError('Select a logical role and a registered provider ID.')
        updated=registry.select(item,value)
        if job:
            stage=next(stage for stage,role in STAGE_ROLES.items() if role==item)
            if p.stage_rows(job_id)[stage]['status']=='DONE':
                raise RuntimeError('This role is already complete; select a provider for a future job instead.')
    p.dump(target,updated.to_config())
    print('Worker configuration saved. No model was started; completed artifacts and usage counters are preserved.')
