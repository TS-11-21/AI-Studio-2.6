"""Portable candidate entry point. No provider installation, login or paid call."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import threading
import urllib.request

ASSET_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
if (ASSET_ROOT / 'app').is_dir():
    ASSET_ROOT = ASSET_ROOT / 'app'
RELEASE_GENERATION = 2
RELEASE_VERSION = '2.6'
DEFAULT_PORT = 43826
DEFAULT_ROOT = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'AIStudio' / 'BilingualPreview-v26'
ALLOWED_MODULES = {'py_compile', 'unittest', 'http.server'}
PUBLIC_SUFFIXES = {'.py','.js','.cjs','.html','.css','.txt','.md','.json','.tscn','.gd','.godot','.svg','.ps1'}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,path)


def source_import(name, root):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name,root/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bind_runtime(root):
    sys.path.insert(0,str(root))
    p = source_import('pipeline',root)
    p.ROOT=root; p.CONFIG=root/'config.local.json'; p.CONTROL=root/'.control'; p.PROFILES=root/'.profiles'
    return p


def prepare(root):
    root = root.absolute()
    if root.resolve()!=root:
        raise ValueError('运行目录不能是重定向目录。')
    root.mkdir(parents=True,exist_ok=True)
    for item in ASSET_ROOT.rglob('*'):
        relative=item.relative_to(ASSET_ROOT)
        if not item.is_file() or item.is_symlink() or item.suffix.lower() not in PUBLIC_SUFFIXES:
            continue
        if any(part.startswith('.') or part in {'jobs','__pycache__','test'} for part in relative.parts):
            continue
        if len(relative.parts)>1 and relative.parts[0] not in {'ui','templates','prompts'} and relative.as_posix() not in {'tools/blender-isolated/bootstrap.py','tools/godot-preauthorized-launch.cjs','tools/godot_extended_acceptance.gd','tools/verify_vfx_pixels.py'}:
            continue
        if item.name in {'config.local.json','workers.local.json','mcp.local.json','auth.json'} or item.name.startswith('test_'):
            continue
        target=root/relative
        if target.resolve()!=target.absolute():
            raise ValueError('运行资源路径被重定向。')
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists() or hashlib.sha256(target.read_bytes()).digest()!=hashlib.sha256(item.read_bytes()).digest():
            shutil.copyfile(item,target)
    config_path=root/'config.local.json'
    if not config_path.exists():
        atomic_json(config_path,{
            'studio_generation':RELEASE_GENERATION,'studio_version':RELEASE_VERSION,'studio_port':DEFAULT_PORT,
            'runtime_private_dir':str(root/'private'),'profiles_dir':str(root/'.profiles'),
            'workers_file':'workers.local.json','godot_path':'','blender_path':'',
            'blender_host':'127.0.0.1','blender_port':9877,
            'claude_base_url':'','claude_auth':'bearer','claude_model':'','codex_model':'',
            'worker_budget_usd_per_job':2.0,'worker_budget_usd_per_call':2.0,
            'claude_budget_usd_per_job':2.0,'claude_budget_usd_per_call':2.0,
            'claude_max_turns':18,'claude_smoke_max_turns':4,'worker_timeout_seconds':1200,
            'max_claude_calls_per_job':2,'max_codex_calls_per_stage':3,'max_research_calls':1,
            'pause_grace_seconds':30,'generate_preview':False,'auto_qa_repair_enabled':False,'auto_qa_repair_max_attempts':1,
            'studio_policy':{'token_saving':True,'auto_retry':False,'max_retries':1,'max_revisions':1,
                             'job_budget_usd':2.0,'connection_budget_usd':0.2,'autosave':True,'notify':False},
            'build_revision':'assistant-compat-20261010','commercial_candidate':True,'paid_sales_enabled':False,'entitlement_connected':False
        })
    bind_runtime(root)
    if not (root/'workers.local.json').exists():
        registry=source_import('worker_adapters',root).default_config()
        atomic_json(root/'workers.local.json',registry)
    if not (root/'mcp.local.json').exists():
        atomic_json(root/'mcp.local.json',{'mcpServers':{}})
    return root


def dispatch_python(arguments, root):
    """Run finite worker/QA processes instead of recursively opening the UI."""
    args=list(arguments)
    isolated=False
    while args and args[0] in {'-u','-I','-B'}:
        option=args.pop(0)
        isolated=isolated or option=='-I'
        if option=='-B': sys.dont_write_bytecode=True
    if not args or (args[0]!='-m' and not args[0].endswith('.py')):
        return False
    if isolated:
        os.environ.pop('PYTHONPATH',None)
        os.environ.pop('PYTHONHOME',None)
        cwd=str(Path.cwd())
        sys.path[:]=[value for value in sys.path if value not in {'',cwd}]
    bind_runtime(root)
    if args[0]=='-m':
        if len(args)<2 or args[1] not in ALLOWED_MODULES:
            raise ValueError('未允许的 Python 模块。')
        sys.argv=[args[1],*args[2:]]
        runpy.run_module(args[1],run_name='__main__',alter_sys=True)
    else:
        entry=Path(args[0]).absolute()
        if entry.resolve()!=entry or not entry.is_file() or not entry.is_relative_to(root):
            raise ValueError('子进程入口必须位于本软件的独立运行目录。')
        if entry.parent!=root and not entry.is_relative_to(root/'jobs'):
            raise ValueError('未允许的子进程入口。')
        sys.argv=[str(entry),*args[1:]]
        runpy.run_path(str(entry),run_name='__main__')
    return True


def serve(root, port, open_browser):
    app_module=source_import('studio_app',root)
    class CandidateApplication(app_module.Application):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            original_state=self.service.get_state
            def visible_state(job_id=None):
                from studio_commerce import public_policy
                value=original_state(job_id)
                value.setdefault('app',{})['commerce']=public_policy(self.service._config())
                return value
            self.service.get_state=visible_state
        def dispatch(self,path,data):
            from studio_commerce import reject_payment,check_application_capacity
            reject_payment(path)
            if path=='/api/commerce/preview':
                from studio_commerce import choose_preview
                return choose_preview(self,data)
            if path=='/api/settings' and isinstance(data,dict) and isinstance(data.get('config'),dict) and 'commercial_preview_tier' in data['config']:
                from studio_commerce import validate_preview_change
                with self.guard,self.transport.lock:
                    validate_preview_change(self,data)
                    return super().dispatch(path,data)
            if path=='/api/job/source-repair':
                from studio_source_repair import request_repair
                return request_repair(self,data)
            if path in ('/api/job/start','/api/job/resume','/api/job/retry'):
                with self.guard:
                    check_application_capacity(self,data['job_id'])
                    return super().dispatch(path,data)
            if path=='/api/store/setup':
                from studio_service import _validate_no_secrets
                _validate_no_secrets(data)
                allowed={'godot_path','godot_mcp_script','blender_path','blender_addon_path','claude_base_url','claude_auth','claude_model','codex_model','codex_command','claude_command','node_command','uvx_command','blender_mcp_command','blender_mcp_args','claude_max_turns','worker_timeout_seconds'}
                if set(data)-allowed: raise ValueError('未知配置字段。')
                for key in ('godot_path','blender_path'):
                    value=data.get(key,'')
                    if value and (not Path(value).is_absolute() or not Path(value).is_file() or Path(value).suffix.lower()!='.exe'):
                        raise ValueError('请填写已安装软件的完整 exe 路径。')
                endpoint=data.get('claude_base_url','')
                if endpoint:
                    from urllib.parse import urlsplit
                    url=urlsplit(endpoint)
                    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                        raise ValueError('网关必须是无凭据的 HTTPS 地址。')
                if data.get('claude_auth','bearer') not in {'bearer','api_key'}: raise ValueError('认证类型无效。')
                self.service._atomic(self.service.config_path,{**self.service._config(),**data})
                return {'saved':True,'paid_calls':0}
            return super().dispatch(path,data)
    application=CandidateApplication(root=root)
    server=app_module.make_server(application,port)
    state_path=root/'.control'/'studio-app.json'
    atomic_json(state_path,{'port':server.server_port,'pid':os.getpid(),'root_id':application.root_id})
    url='http://127.0.0.1:'+str(server.server_port)
    print(json.dumps({'local_url':url,'commercial_ready':False,'model_calls':0}),flush=True)
    application.wakeup.start()
    if open_browser: app_module.open_window(url)
    try: server.serve_forever(poll_interval=.5)
    finally:
        application.pause_on_exit()
        server.server_close()


def main():
    for stream, errors in ((sys.stdin,'strict'),(sys.stdout,'replace'),(sys.stderr,'replace')):
        if stream is not None and hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors=errors)
    # Workers inherit this private data-root reference; it is never a credential.
    inherited=Path(os.environ.get('AI_STUDIO_DATA_ROOT',str(DEFAULT_ROOT))).absolute()
    if dispatch_python(sys.argv[1:],inherited): return
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path,default=DEFAULT_ROOT)
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--no-open',action='store_true')
    parser.add_argument('--port',type=int,default=DEFAULT_PORT)
    args=parser.parse_args()
    root=prepare(args.data_dir)
    os.environ['AI_STUDIO_DATA_ROOT']=str(root)
    if args.check_only:
        module=source_import('studio_app',root)
        application=module.Application(root=root)
        application.service.get_state()
        application.service.get_settings()
        print(json.dumps({'ok':True,'data_root':str(root),'model_calls':0,'login_started':False,
                          'commercial_ready':False,'generation':application.generation}))
        return
    state_file=root/'.control'/'studio-app.json'
    if state_file.exists():
        try:
            saved=json.loads(state_file.read_text(encoding='utf-8'))
            health=json.loads(urllib.request.urlopen('http://127.0.0.1:'+str(saved['port'])+'/api/health',timeout=2).read())
            if health.get('root_id')==hashlib.sha256(str(root).encode()).hexdigest()[:24]:
                if not args.no_open:
                    source_import('studio_app',root).open_window('http://127.0.0.1:'+str(saved['port']))
                return
        except (ValueError,OSError,KeyError): pass
    serve(root,args.port,not args.no_open)


if __name__=='__main__':
    if sys.stdout is None:
        DEFAULT_ROOT.mkdir(parents=True,exist_ok=True)
        sys.stdout=(DEFAULT_ROOT/'candidate-startup.log').open('a',encoding='utf-8')
        sys.stderr=sys.stdout
    main()
