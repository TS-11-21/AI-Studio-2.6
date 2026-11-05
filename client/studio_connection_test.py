"""One explicitly approved Claude smoke request, private stdin only."""
import json
import sys
import time
import pipeline as p
import adapter_runtime as runtime
from worker_adapters import ProviderRegistry
from studio_runner import prepare_shared_schemas

try:
    request=json.loads(sys.stdin.read())
    c=p.config(); registry,_=runtime.read_registry(p,c)
    provider=registry.get_provider(request['provider_id'])
    if provider.adapter!='claude': raise RuntimeError('Unsupported paid connection probe.')
    p.APPROVE_PAID=True
    c['claude_smoke_budget_usd']=min(.2,float(request['limit_usd']))
    c['_provider_secrets']=dict(request.get('secrets',{}))
    for provider_id,value in c['_provider_secrets'].items():
        if isinstance(value,str) and value: p.SECRETS['provider:'+provider_id]=value
    c=runtime.native_config(p,c,provider)
    prepare_shared_schemas(c)
    home=p.profiles_for(c)/'desktop-checks'/provider.provider_id
    runtime.private_profile_path(p,c,home/'mcp.json')
    home.mkdir(parents=True,exist_ok=True)
    c['_claude_role_home']=str(home)
    folder=p.ROOT/'checks'/'desktop-connection'; folder.mkdir(parents=True,exist_ok=True)
    tag='check-'+str(time.time_ns())
    result=p.claude_call(c,folder,'Return status done, summary connection confirmed, empty resource_query and files. No tools or other agents.',tag,blender=False,smoke=True)
    usage=p.load(folder/'.pipeline'/(tag+'.usage.json'))
    print(json.dumps({'connections':{provider.provider_id:{'status':'CONNECTED','detail':result['summary'],'model_tested':True,
          'checked_at':time.time(),'reported_cost_usd':usage.get('reported_total_cost_usd'),'threshold_usd':.2}}},ensure_ascii=False))
except Exception as error:
    print(p.redacted(str(error)))
    raise SystemExit(1)
