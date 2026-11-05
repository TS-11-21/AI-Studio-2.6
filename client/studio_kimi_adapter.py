"""Kimi Code 2.x native CLI bridge. No direct model HTTP/API implementation.

Only managed OAuth membership profiles are supported. Configuration and
protocol fixtures do not prove a logged-in model or MCP end-to-end run.
"""
from __future__ import annotations
import copy,json,os,re,shutil,subprocess,sys,threading,time,tomllib
from pathlib import Path
from urllib.parse import urlsplit

MAX_CONFIG=2*1024*1024
MAX_EVENT=4*1024*1024
MAX_STDOUT=24*1024*1024
OFFICIAL_HOSTS={'api.kimi.com','api.kimi.ai'}
FILE_TOOLS=['Read','Write','Edit']

class OwnedWindowsJob:
    """Closing or killing this bridge terminates its native CLI and MCP tree."""
    def __init__(self,process):
        self.handle=None
        if os.name!='nt':return
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),('LimitFlags',w.DWORD),
                ('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',w.DWORD),
                ('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
        class IO(ctypes.Structure):_fields_=[(name,ctypes.c_ulonglong) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):_fields_=[('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];kernel.CreateJobObjectW.restype=w.HANDLE
        kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD];kernel.SetInformationJobObject.restype=w.BOOL
        kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];kernel.AssignProcessToJobObject.restype=w.BOOL
        kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
        handle=kernel.CreateJobObjectW(None,None);limits=Extended();limits.BasicLimitInformation.LimitFlags=0x2000
        if not handle or not kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle,int(process._handle)):
            if handle:kernel.CloseHandle(handle)
            process.kill();process.wait();raise RuntimeError('Owned Kimi process-tree containment could not be established.')
        self.handle=handle;self.kernel=kernel
    def close(self):
        if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None

def plain(path):
    path=Path(path).absolute()
    if path.resolve()!=path:raise RuntimeError('Kimi profile/workspace cannot be redirected.')
    for item in (path,*path.parents):
        if item.exists() and (item.is_symlink() or getattr(item.stat(),'st_file_attributes',0)&0x400):raise RuntimeError('Kimi profile/workspace reparse path refused.')
    return path

def bounded(path,limit=MAX_CONFIG):
    path=plain(path)
    if not path.is_file() or path.stat().st_size>limit:raise RuntimeError('A bounded existing Kimi profile file is required.')
    return path.read_bytes()

def settings_for(c,provider):
    values=c.get('provider_settings',{})
    if not isinstance(values,dict):raise RuntimeError('Kimi nonsecret profile settings are required.')
    selected={}
    for key in (provider.settings_ref,provider.provider_id):
        if key in values:
            if not isinstance(values[key],dict):raise RuntimeError('Kimi profile settings must be a nonsecret object.')
            selected.update(values[key])
    selected.setdefault('profile_dir',str(Path.home()/'.kimi-code'))
    return selected

def command_for(provider,c):
    command=list(provider.command)
    if command==['kimi']:command=list(c.get('kimi_command',command))
    if len(command)!=1:raise RuntimeError('Kimi native command must name one executable; no pre-added flags or shell wrapper.')
    value=Path(command[0])
    if not value.is_absolute():
        found=shutil.which(command[0])
        if not found:raise RuntimeError('Kimi Code CLI is not installed; no model was started.')
        value=Path(found)
    value=plain(value)
    if not value.is_file() or value.suffix.lower() in ('.cmd','.bat','.ps1'):raise RuntimeError('Use the installed native Kimi executable, not a shell command.')
    return [str(value)]

def profile_spec(c,provider):
    if provider.billing!='subscription':raise RuntimeError('Kimi native support requires explicit managed OAuth membership, not API billing.')
    settings=settings_for(c,provider);source=plain(settings['profile_dir'])
    data=tomllib.loads(bounded(source/'config.toml').decode('utf8'))
    alias=provider.model or data.get('default_model')
    model=data.get('models',{}).get(alias) if isinstance(alias,str) else None
    if not isinstance(model,dict):raise RuntimeError('Select a configured Kimi managed model or complete Kimi login yourself first.')
    name=model.get('provider');account=data.get('providers',{}).get(name)
    if not isinstance(name,str) or not name.startswith('managed:') or not isinstance(account,dict) or account.get('type')!='kimi':raise RuntimeError('Only a Kimi managed member OAuth provider is supported; no API profile fallback.')
    if any(account.get(key) for key in ('api_key','api_key_env','env','custom_headers')) or model.get('base_url') or model.get('protocol'):
        raise RuntimeError('Kimi API keys, environment credentials, custom gateways and model endpoint overrides are refused.')
    endpoint=account.get('base_url','');parsed=urlsplit(endpoint)
    if parsed.scheme!='https' or parsed.hostname not in OFFICIAL_HOSTS or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/')!='/coding/v1':
        raise RuntimeError('Kimi membership profile must use its official coding service without embedded credentials.')
    oauth=account.get('oauth')
    if not isinstance(oauth,dict) or set(oauth)!={'storage','key'} or not all(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]+',value) for value in oauth.values()):
        raise RuntimeError('An explicit managed OAuth reference is required; log in with Kimi Code yourself.')
    filename=settings.get('credential_filename')
    if not filename:
        filename=oauth['key'] if oauth['key'].endswith('.json') else oauth['key']+'.json'
    if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*\.json',filename):raise RuntimeError('Choose one plain managed OAuth credential filename.')
    credential=plain(source/'credentials'/filename)
    if not credential.is_file() or credential.stat().st_size>MAX_CONFIG:raise RuntimeError('Selected Kimi membership OAuth login is missing; operator login/selection is required.')
    allowed=('provider','model','max_context_size','max_input_size','max_output_size','capabilities','support_efforts','default_effort','off_effort','display_name')
    clean={key:copy.deepcopy(model[key]) for key in allowed if key in model}
    if not isinstance(clean.get('max_context_size'),int) or isinstance(clean['max_context_size'],bool) or clean['max_context_size']<=0 or not isinstance(clean.get('model'),str):raise RuntimeError('Kimi managed model metadata is incomplete.')
    if 'tool_use' not in clean.get('capabilities',[]):raise RuntimeError('Selected Kimi model does not declare tool capability.')
    return {'source_profile':str(source),'credential_source':str(credential),'credential_filename':filename,
            'model_alias':alias,'model':clean,'provider_name':name,'provider':{'type':'kimi','base_url':endpoint,'oauth':copy.deepcopy(oauth)}}

def check_config(p,c,provider):
    command=command_for(provider,c);spec=profile_spec(c,provider)
    return {**c,'_kimi_command':command,'_kimi_profile_spec':spec}

def toml_value(value):
    if isinstance(value,bool):return 'true' if value else 'false'
    if isinstance(value,int) and not isinstance(value,bool):return str(value)
    if isinstance(value,str):return json.dumps(value,ensure_ascii=False)
    if isinstance(value,list):return '['+', '.join(toml_value(item) for item in value)+']'
    if isinstance(value,dict):return '{'+', '.join(json.dumps(key)+' = '+toml_value(item) for key,item in value.items())+'}'
    raise RuntimeError('Unsupported Kimi nonsecret profile metadata.')

def prepare_profile(p,c,provider,role,home,tools,cwd):
    from adapter_runtime import private_profile_path,write_private_json
    cfg=check_config(p,c,provider);spec=cfg['_kimi_profile_spec'];home=plain(home);cwd=plain(cwd)
    private_profile_path(p,c,home);home.mkdir(parents=True,exist_ok=True)
    credential=plain(home/'credentials'/spec['credential_filename']);private_profile_path(p,c,credential);credential.parent.mkdir(exist_ok=True)
    shutil.copyfile(plain(spec['credential_source']),credential)
    allowed=FILE_TOOLS+['mcp__'+name+'__*' for name in tools]
    document=('default_model = '+toml_value(spec['model_alias'])+'\ntelemetry = false\nauto_session_title = false\n'
      'merge_all_available_skills = false\nbuiltin_product_skills = false\n'
      '[loop_control]\nmax_steps_per_turn = 20\nmax_attempts_per_step = 1\n'
      '[background]\nmax_running_tasks = 1\nkeep_alive_on_exit = false\nprint_background_mode = "exit"\nprint_max_turns = 1\nprint_wait_ceiling_s = 1\nbash_auto_background_on_timeout = false\n'
      '[tools]\nenabled = '+toml_value(allowed)+'\ndisabled = ["Bash", "Agent", "AgentSwarm", "Skill", "CronCreate", "EnterPlanMode", "ExitPlanMode"]\n'
      '[providers.'+toml_value(spec['provider_name'])+']\n'+''.join(key+' = '+toml_value(value)+'\n' for key,value in spec['provider'].items())+
      '[models.'+toml_value(spec['model_alias'])+']\n'+''.join(key+' = '+toml_value(value)+'\n' for key,value in spec['model'].items()))
    # Paths in tool permissions are explicit. A CLI version interpreting the
    # fallback deny first will stop safely rather than widen filesystem access.
    for name in FILE_TOOLS:
        document+='\n[[permission.rules]]\ndecision = "allow"\npattern = '+toml_value(name+'('+cwd.as_posix()+'/**)')+'\n'
        document+='\n[[permission.rules]]\ndecision = "deny"\npattern = '+toml_value(name)+'\n'
    path=plain(home/'config.toml');private_profile_path(p,c,path);path.write_text(document,encoding='utf8');tomllib.loads(document)
    plain(home/'tui.toml').write_text('[upgrade]\nauto_install = false\n',encoding='utf8')
    write_private_json(p,c,home/'mcp.json',{'mcpServers':tools})
    agent=plain(home/'studio-agent.md')
    agent.write_text('---\nname: ai-studio-isolated\ndescription: Execute one identity-bound studio task within its supplied workspace\ntools: '+json.dumps(allowed)+'\nsubagents: []\n---\n'
       'Only use the supplied workspace '+str(cwd)+'. Use absolute paths for file tools. Never access profiles, credentials or original projects. '
       'Do not create background tasks, call another model, change providers, or run shell commands. Request missing references and exit immediately. '
       'End with the complete identity-bound JSON completion envelope supplied by the controller. A summary alone never means done.\n',encoding='utf8')
    skills=plain(home/'empty-skills');skills.mkdir(exist_ok=True)
    cfg.update(_kimi_role_home=str(home),_kimi_agent=str(agent),_kimi_skills=str(skills))
    return cfg

def clean_env(base,home):
    forbidden=('API_KEY','AUTH_TOKEN','ACCESS_TOKEN','PASSWORD','SECRET','CREDENTIAL')
    env={key:value for key,value in base.items() if not key.upper().startswith(('KIMI_','MOONSHOT_')) and not any(part in key.upper() for part in forbidden)
         and key not in ('CODEX_HOME','CLAUDE_CONFIG_DIR','PYTHONHOME','PYTHONPATH')}
    env.update(KIMI_CODE_HOME=str(home),KIMI_CODE_INFINITE_RETRY='0',KIMI_LOOP_MAX_STEPS_PER_TURN='20',KIMI_LOOP_MAX_ATTEMPTS_PER_STEP='1',
               KIMI_CODE_BACKGROUND_PRINT_BACKGROUND_MODE='exit',KIMI_CODE_BACKGROUND_KEEP_ALIVE_ON_EXIT='false',KIMI_CLI_NO_AUTO_UPDATE='1',KIMI_CODE_WATCH='false')
    return env

def build_args(config,prompt):
    args=list(config['_kimi_command'])+['--prompt',prompt,'--output-format','stream-json','--agent-file',config['_kimi_agent'],'--skills-dir',config['_kimi_skills']]
    if config['_kimi_profile_spec']['model_alias']:args+=['--model',config['_kimi_profile_spec']['model_alias']]
    return args

def content_text(value):
    if isinstance(value,str):return value
    if isinstance(value,list):return ''.join(part.get('text','') for part in value if isinstance(part,dict) and part.get('type')=='text')
    return ''

class KimiStream:
    def __init__(self,emit=None):self.emit=emit or (lambda event:None);self.calls={};self.results=set();self.final=None;self.total=0;self.success=[];self.failed=[];self.rejected=[]
    def feed(self,line):
        self.total+=len(line.encode('utf8'))
        if len(line.encode('utf8'))>MAX_EVENT or self.total>MAX_STDOUT:raise RuntimeError('Kimi output exceeds its bounded protocol size.')
        event=json.loads(line)
        if not isinstance(event,dict):raise RuntimeError('Kimi stream event must be an object.')
        if event.get('role')=='assistant':
            calls=event.get('tool_calls') or []
            if calls:self.final=None
            for call in calls:
                identity=call.get('id');function=call.get('function',{});name=function.get('name')
                if not isinstance(identity,str) or identity in self.calls or not isinstance(name,str):raise RuntimeError('Malformed/duplicate Kimi tool identity.')
                self.calls[identity]=name
                self.emit({'type':'activity','activity':'tool_call','text':'Kimi '+name,'tool_name':name,'level':'info'})
            if not calls:
                text=content_text(event.get('content'))
                try:self.final=json.loads(text)
                except ValueError:self.final=None
        elif event.get('role')=='tool':
            identity=event.get('tool_call_id')
            if identity not in self.calls or identity in self.results:raise RuntimeError('Kimi tool result has no unique matching call.')
            self.results.add(identity);name=self.calls[identity]
            from studio_tool_contracts import tool_result_outcome
            content=event.get('content','');text=content_text(content)
            error=bool(event.get('is_error') or event.get('isError') or event.get('error')) or 'denied by permission' in text.lower()
            outcome=tool_result_outcome({'content':content,'is_error':error},name)
            getattr(self,'success' if outcome=='success' else 'rejected' if outcome=='rejected' else 'failed').append(name)
            self.emit({'type':'activity','activity':'tool_result','text':'Kimi '+name+' '+outcome,'tool_name':name,'level':'info' if outcome=='success' else 'error'})
        # meta records contain no accepted completion and no billable USD claim.
    def finish(self):
        if set(self.calls)!=self.results or not isinstance(self.final,dict):raise RuntimeError('Kimi did not finish with the full structured completion; no guessed DONE.')
        return self.final
    def evidence(self):return {'successful_tool_names':self.success,'failed_tool_names':self.failed,'rejected_tool_names':self.rejected,
      'successful_tool_count':len(self.success),'failed_tool_count':len(self.failed),'rejected_tool_count':len(self.rejected),'source':'Actual native Kimi stream-json tool calls/results; stderr reasoning is excluded.'}

def run_bridge(request):
    config=request['config'];cwd=plain(request['cwd']);tag=request['tag'];receipt=plain(cwd/'.pipeline'/(tag+'.kimi-completion.json'))
    prompt=request['prompt'];parser=KimiStream(lambda event:print(json.dumps(event,ensure_ascii=False),flush=True))
    receipt.parent.mkdir(exist_ok=True)
    args=build_args(config,prompt);env=clean_env(os.environ,config['_kimi_role_home'])
    process=subprocess.Popen(args,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
         encoding='utf8',errors='strict',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    owned=OwnedWindowsJob(process)
    timer=threading.Timer(request['timeout'],lambda:process.kill() if process.poll() is None else None);timer.start()
    try:
        for line in process.stdout:
            if line.strip():parser.feed(line)
        rc=process.wait()
        tools=parser.evidence();plain(cwd/'.pipeline'/(tag+'.tools.json')).write_text(json.dumps(tools,ensure_ascii=False,indent=2),encoding='utf8')
        if rc:raise RuntimeError('Native Kimi invocation failed or timed out; no provider fallback.')
        completion=parser.finish()
        # Monetary amounts written by the model are not supplier accounting.
        completion.pop('usage',None);completion['usage']={'reported_cost_usd':None,'currency':'USD'}
        receipt.write_text(json.dumps(completion,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps({'type':'activity','activity':'completion_received','text':'Kimi 已返回结构化交接；控制器将核对身份与真实文件。'},ensure_ascii=False),flush=True)
        return 0
    finally:
        timer.cancel()
        owned.close()
        if process.poll() is None:process.kill();process.wait()

def call(p,c,cwd,prompt,tag,plan):
    from worker_adapters import completion_schema,decode_completion
    request={'config':c,'cwd':str(cwd),'tag':tag,'timeout':p.positive_runtime_setting(c,'worker_timeout_seconds',1200)}
    # Pass only the required native profile metadata to the bridge, never the
    # parent provider-secret dictionary or any unrelated configuration.
    request['config']={key:c[key] for key in ('_kimi_command','_kimi_role_home','_kimi_agent','_kimi_skills','_kimi_profile_spec')}
    request['prompt']=prompt+'\nEND THIS INVOCATION WITH EXACT JSON ONLY:\n'+json.dumps(completion_schema(plan),ensure_ascii=False)+'\nTASK IDENTITY: '+json.dumps(plan.task.to_dict(),ensure_ascii=False)
    command=[sys.executable,str(Path(__file__).resolve()),'--bridge']
    try:
        rc,out,err=p.process(command,cwd,p.env_for('none',c),prompt=json.dumps(request,ensure_ascii=False),timeout=request['timeout']+15,log=cwd/'.pipeline'/(tag+'.log'))
        if rc:raise RuntimeError('Kimi CLI did not complete its native worker call; inspect sanitized activity and retained files.')
        receipt=plain(cwd/'.pipeline'/(tag+'.kimi-completion.json'))
        completion=json.loads(bounded(receipt).decode('utf8'));decoded=decode_completion(plan,completion)
        tools=p.load(cwd/'.pipeline'/(tag+'.tools.json'));names=tools['successful_tool_names']
        if decoded['result']['status']=='done' and plan.task.role=='vfx' and c.get('_task_kind')!='software' and 'mcp__blender__execute_blender_code' not in names:
            raise RuntimeError('Native Kimi VFX completed without actual successful Blender code evidence.')
        if decoded['result']['status']=='done' and plan.task.role in ('gameplay','integration') and c.get('_task_kind')!='software' and not any(name.startswith('mcp__godot__') for name in names):
            raise RuntimeError('Native Kimi game completion has no actual Godot MCP evidence.')
        if tools['rejected_tool_count'] or any(name.startswith('mcp__') for name in tools['failed_tool_names']):raise RuntimeError('Kimi native MCP rejection/error cannot be reported as success.')
        p.dump(cwd/'.pipeline'/(tag+'.result.json'),decoded['result']);return decoded
    finally:
        p.dump(cwd/'.pipeline'/(tag+'.usage.json'),{'invocation_tag':tag,'billing_mode':'subscription','model_alias':c['_kimi_profile_spec']['model_alias'],'reported_total_cost_usd':None,
             'cost_known':False,'note':'Managed Kimi membership CLI has no verified per-call USD invoice in this bridge; quota is controlled by the assistant account, not fabricated as USD0.'})

if __name__=='__main__':
    if sys.argv[1:]!=['--bridge']:raise SystemExit('This module is a controller-owned native bridge, not an installer or direct model API.')
    try:raise SystemExit(run_bridge(json.loads(sys.stdin.read())))
    except Exception:
        print(json.dumps({'type':'activity','level':'error','activity':'native_worker_failed','text':'Kimi 原生调用未完成，保留文件/真实工具记录；未自动重试或切换助手。'},ensure_ascii=False),flush=True)
        raise SystemExit(1)
