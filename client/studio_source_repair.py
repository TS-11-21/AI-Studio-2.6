"""Audited source-image persistence correction. No arbitrary tasks or code API."""
from __future__ import annotations
import contextlib, hashlib, importlib.util, json, os, shutil, subprocess, sys, threading, time
from decimal import Decimal
from pathlib import Path

SCHEMA='ai-studio-source-persistence-repair/v1'
MAX_CALL=Decimal('.30')
_ACTIVE_ENGINE=None

def read(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def plain(path):
    path=Path(path).absolute()
    if path.resolve()!=path: raise ValueError('Redirected source repair path is refused.')
    for item in (path,*path.parents):
        if item.exists() and (item.is_symlink() or getattr(item.stat(),'st_file_attributes',0)&0x400):
            raise ValueError('Reparse source repair path is refused.')
    return path
def dump(path,value):
    path=plain(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+str(time.time_ns())+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf8');os.replace(temp,path)
def dec(value):
    if isinstance(value,bool):raise ValueError('A numeric USD allowance is required.')
    result=Decimal(str(value))
    if not result.is_finite() or result<0:raise ValueError('Invalid USD allowance.')
    return result
def frames(art):
    return {'frames/frame_%04d.png'%i:digest(plain(art/'frames'/('frame_%04d.png'%i))) for i in range(1,19)}
def game_files(game):
    from studio_source_repair_evidence import game_code_hashes
    return game_code_hashes(game)

def target(root,job,completion_only=False):
    root=plain(root);job=plain(job)
    if not job.is_relative_to(root/'jobs'):raise ValueError('Repair must belong to this application job.')
    if (job/'pipeline.v3.json').is_file():
        plan=read(job/'pipeline.v3.json');state=read(job/'.graph-state.json');kind='v3'
        candidates=[(s,m) for s in plan['stations'] if s['kind']=='vfx' for m in s['members']
            if plan['providers'][m['provider_id']]['adapter']=='claude' and plan['providers'][m['provider_id']]['billing']=='metered']
        if len(candidates)!=1:raise ValueError('Exactly one original native Claude art member is required.')
        station,member=candidates[0];row=state['members'][member['id']];run=row['runs']['base']
        workspace=plain(run['workspace']);canonical=plain(Path(state['stations'][station['id']]['runs']['base']['output'])/'art')
        final=plan.get('delivery_station_id')
        if not final:raise ValueError('A completed real delivery is required.')
        game=plain(Path(state['stations'][final]['runs']['base']['output'])/'game')
    elif (job/'pipeline.v2.json').is_file():
        plan=read(job/'pipeline.v2.json');state=read(job/'.team-state.json');kind='v2'
        candidates=[m for m in plan['roles']['vfx']['members'] if plan['providers'][m['provider_id']]['adapter']=='claude'
            and plan['providers'][m['provider_id']]['billing']=='metered']
        if len(candidates)!=1:raise ValueError('Exactly one original native Claude art member is required.')
        member=candidates[0];row=state['members'][member['id']];workspace=plain(row['workspace']);canonical=job/'art';game=job/'game'
        station=None
    else:
        import pipeline as p,adapter_runtime as ar
        plan=read(job/'task.json');kind='v1';state=None;workspace=job;canonical=job/'art';game=job/'game';station=None
        provider=ar.provider_for(p,p.config(),job,'claude_vfx')
        if provider.adapter!='claude' or provider.billing!='metered':raise ValueError('Original art provider must be native Claude.')
        member={'id':None,'provider_id':provider.provider_id}
        row=p.stage_rows(job.name)['claude_vfx']
    if plan.get('kind','fan-wave-demo')!='fan-wave-demo':raise ValueError('This correction is limited to the fan-wave template.')
    expected='FAILED' if completion_only else 'DONE'
    if row.get('status')!=expected:raise ValueError('The original Claude producer is not in the audited operation state.')
    if not workspace.is_relative_to(job):raise ValueError('Member workspace escapes its job.')
    art=plain(workspace/'art');source=plain(art/'source.blend')
    if not source.is_file() or (not completion_only and digest(source)!=digest(plain(canonical/'source.blend'))):raise ValueError('Candidate/canonical source bytes differ.')
    return {'root':root,'job':job,'kind':kind,'plan':plan,'state':state,'workspace':workspace,'art':art,'canonical':canonical,
        'game':plain(game),'source':source,'member':member,'station':station,'row':row}

def prior_operation(t):
    candidates=[]
    for folder in plain(t['job']/'.source-repair/history').iterdir():
        plain(folder);record_file=plain(folder/'authorized-operation.json')
        if record_file.is_file():
            record=read(record_file)
            if record.get('schema')==SCHEMA and record.get('job_id')==t['job'].name and record.get('operation_mode','source_only')=='source_only':candidates.append((record.get('requested_at',0),folder,record))
    if len(candidates)!=1:raise ValueError('Exactly one original audited source correction history is required.')
    _,folder,record=candidates[0];t['history']=folder
    return record

def check_evidence(t,audit_file,completion_only=False):
    if completion_only:
        from studio_source_completion_evidence import validate_completion_evidence
        record=prior_operation(t)
        return validate_completion_evidence(t,audit_file,record)
    from studio_source_repair_evidence import validate_evidence
    return validate_evidence(t,audit_file)

def verify_budget_stop(t):
    folder=plain(t['art']/'.pipeline');usage=read(plain(folder/'claude_vfx-2.usage.json'));tools=read(plain(folder/'claude_vfx-2.tools.json'))
    if usage.get('is_error') is not True or usage.get('subtype')!='error_max_budget_usd' or usage.get('cost_known') is not True or usage.get('invocation_tag')!='claude_vfx-2' or usage.get('is_smoke') is not False:
        raise ValueError('Only the known-cost actual second-invocation budget stop can continue.')
    if dec(usage.get('reported_total_cost_usd'))<=0 or dec(usage.get('job_cap_usd'))!=1:raise ValueError('Invalid original budget-stop cost evidence.')
    names=tools.get('successful_tool_names',[])
    if tools.get('rejected_tool_count',0) or tools.get('failed_tool_count',0) or 'StructuredOutput' in names or 'mcp__blender__execute_blender_code' not in names:
        raise ValueError('Budget-stop completion requires successful original Blender save tools and no formal result/rejections.')
    if (folder/'claude_vfx-2.result.json').exists():raise ValueError('The original budget-stopped worker already has a formal result.')
    return usage

def validate_budget(snapshot,approval,provider,member_id,completion_only=False):
    if approval.get('approved') is not True or approval.get('providers')!=[provider]:raise ValueError('Explicit approval of the original Claude provider is required.')
    if member_id and approval.get('members')!=[member_id]:raise ValueError('Explicit original member approval is required.')
    if snapshot.get('unknown_invocations') or snapshot.get('interrupted_invocations') or snapshot.get('cost_reconciliation_required'):
        raise ValueError('Unknown/interrupted fees must be reconciled before source correction.')
    if dec(snapshot.get('reserved_usd',0)):raise ValueError('Active reservations forbid source correction.')
    total=dec(snapshot.get('total_cap_usd',snapshot.get('job_cap_usd',snapshot.get('cap_usd'))))
    approved=dec(approval.get('job_budget_usd'))
    cap=dec(approval.get('source_repair_cap_usd'))
    maximum=Decimal('.20') if completion_only else MAX_CALL
    if total!=1 or approved!=total or not Decimal('0')<cap<=maximum:raise ValueError('Keep the original USD1 total and the narrow operation invocation allowance.')
    remaining=total-dec(snapshot.get('spent_usd',0))
    if remaining<cap+Decimal('.10'):raise ValueError('Insufficient remaining allowance including settlement buffer.')
    return cap

def validate_free_finalize(snapshot,approval):
    if approval.get('approved') is not False:raise ValueError('Host-only finalization must explicitly decline new paid calls.')
    if approval.get('providers') or approval.get('members'):raise ValueError('Host-only finalization grants no model/provider/member dispatch.')
    if snapshot.get('unknown_invocations') or snapshot.get('interrupted_invocations') or snapshot.get('cost_reconciliation_required') or dec(snapshot.get('reserved_usd',0)):
        raise ValueError('Known settled accounting is required before finalizing saved formal results.')
    total=dec(snapshot.get('total_cap_usd',snapshot.get('job_cap_usd',snapshot.get('cap_usd'))))
    if total!=1 or dec(approval.get('job_budget_usd'))!=total or dec(snapshot.get('spent_usd',0))>total:
        raise ValueError('Original settled USD1 accounting must remain within its unchanged total.')

def verify_formal_usage(t):
    usage=read(plain(t['art']/'.pipeline/claude_vfx-3.usage.json'))
    if usage.get('invocation_tag')!='claude_vfx-3' or usage.get('is_error') is not False or usage.get('cost_known') is not True or usage.get('is_smoke') is not False:
        raise ValueError('A real completed, known-cost third invocation is required for host-only finalization.')
    if dec(usage.get('reported_total_cost_usd'))<0 or dec(usage.get('job_cap_usd'))!=1:raise ValueError('Invalid existing formal invocation accounting.')
    return usage

INSPECT_CODE='''import bpy,hashlib,json
s=bpy.context.scene
data={'packed':[],'job':s.get('ai_studio_job'),'isolated':s.get('ai_studio_isolated_test'),'objects':sorted([(o.name,o.type) for o in s.objects]),'actions':sorted(a.name for a in bpy.data.actions),'frame_range':[s.frame_start,s.frame_end],'fps':s.render.fps,'resolution':[s.render.resolution_x,s.render.resolution_y,s.render.resolution_percentage],'film_transparent':s.render.film_transparent,'camera_type':s.camera.data.type if s.camera else None}
for image in bpy.data.images:
 if image.packed_file:data['packed'].append({'file':bpy.path.basename(image.filepath),'sha256':hashlib.sha256(bytes(image.packed_file.data)).hexdigest(),'fake_user':image.use_fake_user})
with open(__OUTPUT__,'w',encoding='utf8') as f:json.dump(data,f)
'''

def inspect_saved(t,config,label):
    folder=plain(t['job']/'.source-repair');folder.mkdir(exist_ok=True)
    suffix='-'+str(time.time_ns())
    output=plain(folder/(label+suffix+'-native.json'));script=plain(folder/(label+suffix+'-inspect.py'))
    if output.exists() or script.exists():raise ValueError('Native inspector leaves must be newly created files.')
    script.write_text(INSPECT_CODE.replace('__OUTPUT__',repr(str(output))),encoding='utf8')
    before=digest(t['source'])
    env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('KEY','TOKEN','SECRET','PASSWORD','CREDENTIAL'))}
    done=subprocess.run([config['blender_path'],'--factory-startup','--background','--disable-autoexec',str(t['source']),'--python-exit-code','1','--python',str(script)],
        capture_output=True,encoding='utf8',errors='replace',env=env,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if done.returncode or not output.is_file() or digest(t['source'])!=before:raise ValueError('Readonly native source reopen did not complete safely.')
    native=read(output)
    native['inspected_source_sha256']=before
    alias=t['job'].name if t['kind']=='v1' else read(plain(t['workspace']/'.studio-runtime.json'))['job_id']
    if native.get('isolated') is not True or native.get('job')!=alias:raise ValueError('Saved source lacks its exact original isolated identity.')
    return native

def original_config(t):
    if t['kind']=='v1':
        import pipeline as p
        return p.config()
    return t['plan']['config']

def request_lease(t):
    if t['kind']=='v1':
        import pipeline as p
        return p.runner_lock()
    if t['kind']=='v2':
        from studio_v2_engine import pipeline_lease
    else:
        from studio_v3_engine import pipeline_lease
    return pipeline_lease(t['job'])

def request_repair(application,data):
    allowed={'job_id','audit_file','confirmed','paid_approval','completion_only','finalize_only'}
    if set(data)-allowed or data.get('confirmed') is not True:raise ValueError('A confirmed, audited source-only correction is required; arbitrary code/options are refused.')
    completion=data.get('completion_only',False)
    finalize=data.get('finalize_only',False)
    if not isinstance(completion,bool) or not isinstance(finalize,bool):raise ValueError('Narrow operation modes must be explicitly boolean.')
    completion=completion or finalize
    service=application.service;job=service._job(data['job_id'])
    if importlib.util.find_spec('store_cancel') is not None:
        from store_cancel import reject_cancelled
        reject_cancelled(application,job.name)
    with service._lock_for(job.name),application.guard:
        from studio_commerce import check_application_capacity
        check_application_capacity(application,job.name)
        if application.transport.get_job_runtime(job.name).get('running'):raise ValueError('Pause and wait for all owned workers to exit first.')
        if (job/'pause.request').exists():raise ValueError('Explicit pause must be cleared through normal resume before this correction.')
        t=target(application.root,job,completion)
        with request_lease(t):
            t=target(application.root,job,completion);audit=check_evidence(t,data['audit_file'],completion)
            expected=3 if finalize else 2 if completion else 1
            if t['row'].get('attempts')!=expected:raise ValueError('Only the exact audited original invocation sequence can continue.')
            if completion:verify_budget_stop(t)
            budget=application.transport.get_budget(job.name)
            if finalize:
                validate_free_finalize(budget,data['paid_approval']);verify_formal_usage(t);formal_evidence(t,3,True);cap=Decimal('0')
            else:cap=validate_budget(budget,data['paid_approval'],t['member']['provider_id'],t['member']['id'],completion)
            native=inspect_saved(t,original_config(t),'request-before')
            if completion:
                old=prior_operation(t)
                verify_saved_packs(native,old)
            elif native['packed']:raise ValueError('This saved source no longer has the audited zero-packed failure.')
            record={'schema':SCHEMA,'job_id':job.name,'source_sha256':digest(t['source']),'audit_sha256':digest(data['audit_file']),
                'png_sha256':frames(t['art']),'game_code_sha256':game_files(t['game']),'cap_usd':str(cap),'requested_at':time.time(),
                'provider_id':t['member']['provider_id'],'member_id':t['member']['id'],
                'native_before':{k:v for k,v in native.items() if k not in ('packed','inspected_source_sha256')},
                'operation_mode':'finalize_only' if finalize else 'completion_only' if completion else 'source_only'}
        request={**data,'completion_only':completion,'finalize_only':finalize,'action':'source-repair','source_repair_record':record}
        with service._guard:
            prior=service._threads.get(job.name)
            if prior and prior.is_alive():raise ValueError('A worker already owns this job.')
            service._runtime[job.name]={'status':'RUNNING','action':'source-repair','started_at':time.time()}
        application.wakeup.forget(job.name)
        def work():
            try:result=run_process(application.transport,request,lambda e:service._events_for(job.name,e))
            except Exception as error:result={'status':'FAILED'};service._events_for(job.name,{'type':'runner','status':'failed','level':'error','text':str(error)})
            with service._guard:service._runtime[job.name].update(status=str(result['status']).upper(),finished_at=time.time())
        thread=threading.Thread(target=work,name='source-repair-'+job.name,daemon=True)
        with service._guard:service._threads[job.name]=thread
        thread.start()
        return {'job_id':job.name,'status':'RUNNING','operation':'host-only formal result finalization' if finalize else 'source-only packing persistence repair','invocation_cap_usd':float(cap),'model_dispatch_enabled':not finalize,'new_pipeline_created':False}

def run_process(transport,request,event_callback):
    import pipeline as p
    with transport.lock:
        from studio_commerce import check_local_capacity
        check_local_capacity(transport,request['job_id'])
        if transport.get_job_runtime(request['job_id']).get('running'):raise ValueError('A process already owns this job.')
        payload={**request,'secrets':transport.secrets.snapshot()}
        env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY','AUTH_TOKEN','ACCESS_TOKEN','PASSWORD','SECRET','CREDENTIAL'))}
        process=subprocess.Popen([transport.python,'-u',str(transport.root/'studio_source_repair.py')],cwd=str(transport.root),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf8',errors='replace',env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        transport.processes[request['job_id']]=process
        process.stdin.write(json.dumps(payload,ensure_ascii=False)+'\n');process.stdin.close()
    status='failed'
    try:
        for line in iter(process.stdout.readline,''):
            safe=transport.secrets.redact(p.redacted(line))
            try:event=json.loads(safe)
            except ValueError:event={'type':'activity','text':safe.strip()[:1800],'level':'info'}
            if not isinstance(event,dict):continue
            if event.get('type')=='runner':status=event.get('status',status)
            event_callback(event)
        code=process.wait()
        return {'status':status,'exit_code':code}
    finally:
        with transport.lock:transport.processes.pop(request['job_id'],None)

def repair_prompt(t,alias):
    from studio_tool_contracts import BLENDER_MARKER,BLENDER_CAPABILITIES
    template=Path(t['root']/'templates/blender_pack_existing_frames.py').read_text(encoding='utf8')
    template=template.replace('__EXPECTED_SOURCE_LITERAL__',repr(str(t['source']))).replace('__EXPECTED_JOB_LITERAL__',repr(alias))
    return ('This is an explicitly approved SOURCE-ONLY persistence correction of the existing formally delivered art. '
        'Do not render, generate PNGs, clear/add objects, edit materials/keyframes, edit gameplay, or invoke any other model/provider. '
        'Check the existing exact source/isolation/alias. Execute only this already validated bpy-only load/fake_user/pack/save contract. '
        'Use the existing 18 PNGs unchanged. Do not claim persistent packs have been independently verified; the controller reopens the saved disk file. '
        'Update HANDOFF.md with this limited correction. If manifest metadata declares the old source checksum or packing status, update it honestly through file tools; never invent a checksum. Keep the original animation/frame contract unchanged. '
        'Then formal StructuredOutput status done, resource_query empty, and concrete existing source.blend/manifest.json/HANDOFF.md/18 frames paths. '\
        'No hidden/private files in public files. Keep the response concise.\n'+BLENDER_MARKER+'\n'+BLENDER_CAPABILITIES+'\nSOURCE-ONLY CONTRACT:\n'+template)

def completion_prompt(t,record):
    from studio_tool_contracts import BLENDER_MARKER
    names=['source.blend','manifest.json','HANDOFF.md']+list(record['png_sha256'])
    return (BLENDER_MARKER+'\nAudited COMPLETION-ONLY continuation of the original Claude art producer after its known-cost budget stop. '
        'The original Claude already saved the source; an independent trusted disk reload verified all 18 exact original PNG packs with fake_user, original scene/actions/settings, PNG and game bytes unchanged. '
        'No Blender/MCP tools are available in this call. Do not edit source.blend, PNGs, manifest, gameplay, geometry, animation or any existing resource. '
        'Only Read existing HANDOFF.md if needed, then Write/Edit HANDOFF.md to report this limited persistence correction and the independent controller evidence; do not claim new rendering. '
        'Then immediately call the required StructuredOutput tool with status done, concise truthful summary, empty resource_query, and these existing exact relative files. No other work or model. '
        'The controller repeats disk/source/frame/game verification and real QA after your formal result.\nFILES: '+json.dumps(names)+'\n'
        'VERIFIED SOURCE SHA256: '+record['source_sha256']+'\n')

def configure_completion(module,config):
    """Keep normal native worker/accounting; narrow this one CLI to file tools."""
    empty=plain(module.CONTROL/'empty.mcp.json')
    if not empty.is_file() or read(empty)!={'mcpServers':{}}:raise ValueError('A trusted empty MCP configuration is required.')
    original=module.claude_call
    def file_only(cfg,cwd,prompt,tag,*,blender=True,smoke=False):
        cfg={**cfg,'_claude_role_mcp':str(empty),'_claude_extra_mcp_tools':[],'_claude_tool_role':'completion-only'}
        return original(cfg,cwd,prompt,tag,blender=False,smoke=smoke)
    module.claude_call=file_only
    config['_source_repair_completion_only']=True

def archive_delivery(t,record):
    history=plain(t['job']/'.source-repair'/'history'/str(time.time_ns()));history.mkdir(parents=True)
    for name in ('delivery',):
        source=plain(t['job']/name)
        if source.is_dir():
            for item in source.rglob('*'):plain(item)
            shutil.copytree(source,plain(history/name))
    shutil.copy2(plain(t['source']),plain(history/'source-before.blend'))
    failed=plain(t['job']/'.source-repair/FAILED.json')
    if failed.is_file():shutil.copy2(failed,plain(history/'prior-operation-failed.json'))
    if t['state'] is not None:dump(history/'state-before.json',t['state'])
    dump(history/'authorized-operation.json',record)
    dump(t['job']/'delivery'/'SOURCE_REPAIR_PENDING.json',{'status':'STALE','reason':'Independent saved-source packing persistence failure; same-job source correction running.','history':str(history)})
    dump(t['job']/'delivery'/'DELIVERY_STALE.json',{'status':'STALE','reason':'Saved-source packing persistence correction requires new actual QA and ZIP.','archive':str(history)})
    return history

def formal_evidence(t,attempt,completion_only=False):
    folder=t['art']/'.pipeline';result=read(folder/('claude_vfx-%d.result.json'%attempt));tools=read(folder/('claude_vfx-%d.tools.json'%attempt))
    names=tools.get('successful_tool_names',[])
    if result.get('status')!='done' or 'StructuredOutput' not in names or (not completion_only and 'mcp__blender__execute_blender_code' not in names):
        raise ValueError('Actual normal Claude worker did not formally deliver the source correction.')
    if completion_only and any(name.startswith('mcp__') for name in names+tools.get('failed_tool_names',[])+tools.get('rejected_tool_names',[])):
        raise ValueError('Completion-only continuation attempted a forbidden MCP action.')
    if tools.get('rejected_tool_count',0):raise ValueError('Source correction contains rejected native tools.')
    if tools.get('failed_tool_count',0):
        from studio_recovered_handoff import recovered_optional_handoff
        if not recovered_optional_handoff(t,attempt,tools):raise ValueError('Source correction contains unresolved failed tools.')
    return result

def verify_saved_packs(native,record):
    if {k:v for k,v in native.items() if k not in ('packed','inspected_source_sha256')}!=record['native_before']:
        raise ValueError('Source correction changed original geometry/action identities or scene settings.')
    expected={Path(n).name:h for n,h in record['png_sha256'].items()}
    actual={row['file']:row['sha256'] for row in native['packed']}
    if actual!=expected or len(native['packed'])!=18 or not all(row['fake_user'] for row in native['packed']):raise ValueError('Saved disk source does not persist all 18 exact original PNG packs.')

def complete_source(t,config,attempt,record,completion_only=False):
    result=formal_evidence(t,attempt,completion_only)
    if frames(t['art'])!=record['png_sha256'] or game_files(t['game'])!=record['game_code_sha256']:raise ValueError('Source-only correction changed PNGs or gameplay.')
    if completion_only:
        if digest(t['source'])!=record['source_sha256']:raise ValueError('Completion-only continuation changed the already verified saved source.')
    elif digest(t['source'])==record['source_sha256']:raise ValueError('The saved source did not change.')
    native=inspect_saved(t,config,'completed-after')
    verify_saved_packs(native,record)
    dump(t['job']/'.source-repair'/'PERSISTENCE_VERIFIED.json',{'schema':SCHEMA,'verified':True,'native':native,
        'source_sha256':digest(t['source']),'pngs_unchanged':True,'gameplay_unchanged':True,'actual_claude_attempt':attempt})
    return result

def _run_payload(request):
    global _ACTIVE_ENGINE
    _ACTIVE_ENGINE=None
    import pipeline as p,adapter_runtime as ar,studio_runner as live
    root=Path(p.ROOT);job=p.job_path(request['job_id']);live._JOB=job
    emit=live.emit
    completion=request.get('completion_only',False)
    finalize=request.get('finalize_only',False)
    t=target(root,job,completion);check_evidence(t,request['audit_file'],completion);record=request['source_repair_record'];approval=request['paid_approval']
    secrets=request.get('secrets',{});engine=None
    if t['kind']=='v2':
        from studio_v2_engine import V2Engine
        engine=V2Engine(root,job,t['plan'],approval=approval,secrets=secrets,event_callback=emit)
        lease=engine.lease()
    elif t['kind']=='v3':
        from studio_v3_engine import GraphEngine
        engine=GraphEngine(root,job,t['plan'],approval=approval,secrets=secrets,event_callback=emit)
        lease=engine.lease()
    else:
        lease=p.runner_lock()
    _ACTIVE_ENGINE=engine
    with lease:
        t=target(root,job,completion);check_evidence(t,request['audit_file'],completion)
        if completion:verify_budget_stop(t)
        if finalize:verify_formal_usage(t);formal_evidence(t,3,True)
        if digest(t['source'])!=record['source_sha256'] or frames(t['art'])!=record['png_sha256'] or game_files(t['game'])!=record['game_code_sha256']:
            raise ValueError('Audited source/PNG/gameplay changed before the actual worker acquired ownership.')
        runtime=job/'.studio-runner.json';dump(runtime,{'pid':os.getpid(),'status':'running','job_id':job.name,'started_at':time.time(),'operation':SCHEMA})
        history=archive_delivery(t,record);emit({'type':'runner','status':'running','activity':'已存旧交付历史；原Claude进行同任务源持久化修正，不重新绘帧。'})
        stage='claude_vfx';attempt=t['row']['attempts'] if finalize else t['row']['attempts']+1
        if attempt!=(3 if completion else 2):raise ValueError('This narrow tool allows only the exact audited original producer invocation.')
        if engine:
            member=engine.members[t['member']['id']]
            ctx=engine.context(member,'production') if t['kind']=='v2' else engine.context(member,'production','base')
            config=ctx.config;module=ctx.module;candidate=ctx.job
            if finalize:validate_free_finalize(engine.ledger.snapshot(),approval)
            else:validate_budget(engine.ledger.snapshot(),approval,member['provider_id'],member['id'],completion)
            config['_v2_call_cap']=Decimal('0') if finalize else dec(approval['source_repair_cap_usd']);config['worker_budget_usd_per_call']=float(MAX_CALL)
            if not completion:engine.preflight(ctx)
            alias=record['native_before']['job'] if completion else config.get('_v2_blender_identity',config.get('_graph_blender_identity'))
            fixed_prompt=completion_prompt(t,record) if completion else repair_prompt(t,alias)
            config['_graph_prompt']=fixed_prompt;config['_v2_prompt_extra']='';config['_source_repair_operation']=True
            old=module.base_prompt;module.base_prompt=lambda job,role:fixed_prompt if role in ('vfx','claude_vfx') else old(job,role)
            if t['kind']=='v2':
                row=engine.state['members'][member['id']];row.update(status='RUNNING',phase='production',attempts=attempt)
                if not finalize:row['phase_attempts']['production']=row['phase_attempts'].get('production',0)+1
                engine.state['stages']['claude_vfx']['status']='RUNNING';engine.state['stages']['qa']['status']='DORMANT'
            else:
                row=engine.row(member,'base');row.update(status='RUNNING',phase='production',attempts=attempt)
                if not finalize:row['phase_attempts']['production']=row['phase_attempts'].get('production',0)+1
                engine.state['members'][member['id']]['attempts']=attempt;engine.state['qa']['status']='DORMANT'
            engine.state['status']='RUNNING';engine.persist()
        else:
            import studio_blender,studio_tasks
            module=p;candidate=job;config=p.config();studio_tasks.install(p,config,job)
            for name,value in secrets.items():
                if isinstance(value,str):p.SECRETS['provider:'+name]=value
            p.SECRETS['claude']=secrets.get('claude-code','');p.APPROVE_PAID=not finalize
            config['_provider_secrets']=secrets;config['worker_budget_usd_per_job']=1;config['claude_budget_usd_per_job']=1
            config['profiles_dir']=str(p.profiles_for(config)/'studio-jobs'/job.name)
            config['worker_budget_usd_per_call']=0.0 if finalize else float(dec(approval['source_repair_cap_usd']));config['claude_budget_usd_per_call']=config['worker_budget_usd_per_call']
            if finalize:validate_free_finalize(ar.ledger_for(p,config,job).snapshot(),approval)
            else:validate_budget(ar.ledger_for(p,config,job).snapshot(),approval,t['member']['provider_id'],None,completion)
            if not completion:
                port=studio_blender.ensure_instance(root,job.name,config,emit);config.update(blender_port=port,blender_mcp_args=['--host','127.0.0.1','--port',str(port)])
            config['_source_repair_operation']=True
            alias=job.name;p.process=live.live_process;live.prepare_shared_schemas(config)
            old=p.base_prompt;p.base_prompt=lambda job,role:(completion_prompt(t,record) if completion else repair_prompt(t,alias)) if role in ('vfx','claude_vfx') else old(job,role)
            p.set_stage(job.name,'qa','DORMANT','Saved-source persistence failed independent reload; prior QA retained in history.')
            p.set_stage(job.name,'claude_vfx','RUNNING','Audited source correction or finalization of actual original Claude result; prior counters preserved.',increment=not finalize)
        try:
            if finalize:result=read(plain(t['art']/'.pipeline/claude_vfx-3.result.json'))
            else:
                if completion:configure_completion(module,config)
                result=ar.worker(module,config,candidate,stage,attempt)
            module.validate_result(result)
            if result['status']!='done':raise ValueError('The actual Claude source correction is incomplete: '+result.get('summary',''))
            result=complete_source(t,config,attempt,record,completion)
            if engine:
                row.update(status='DONE',summary=result['summary'],error='',files=list(dict.fromkeys(t['row'].get('files',[])+result['files'])),result=result)
                if t['kind']=='v2':
                    engine.state['stages']['claude_vfx']['status']='DONE';engine._publish('vfx')
                else:
                    engine.publish(t['station']['id'],'base')
            else:
                p.set_stage(job.name,'claude_vfx','DONE',result['summary']);p.publish_bundle(job)
            # Refresh only the saved source accompanying already accepted game textures.
            source_in_game=plain(t['game']/'assets/fan_wave_001/source.blend')
            if source_in_game.is_file():shutil.copy2(t['source'],source_in_game)
            if frames(t['canonical'])!=record['png_sha256'] or game_files(t['game'])!=record['game_code_sha256']:raise ValueError('Publishing changed protected production bytes.')
            if engine:
                engine.persist();engine.acceptance()
                done=engine.state['stages']['qa']['status']=='DONE' if t['kind']=='v2' else engine.state['qa']['status']=='DONE'
                if not done:raise ValueError('Actual native post-correction QA did not pass.')
                engine.state['status']='DONE'
                engine.persist()
            else:
                p.set_stage(job.name,'qa','RUNNING','Real acceptance after saved-source correction.',increment=True)
                p.qa(config,job);p.deliver(config,job);p.set_stage(job.name,'qa','DONE','Real source correction and acceptance completed.')
            (job/'delivery/SOURCE_REPAIR_PENDING.json').unlink(missing_ok=True)
            plain(job/'delivery/DELIVERY_STALE.json').unlink(missing_ok=True)
            dump(job/'.source-repair/COMPLETED.json',{'schema':SCHEMA,'status':'DONE','attempt':attempt,'source_sha256':digest(t['source']),
                'pngs_unchanged':True,'gameplay_unchanged':True,'history':str(history),'updated_at':time.time(),'host_finalize_only':finalize,'new_model_calls':0 if finalize else 1})
            emit({'type':'runner','status':'completed','activity':'原Claude已正式修正18张原PNG的源持久化；磁盘重载、实际QA和新ZIP完成。'})
            dump(runtime,{'pid':os.getpid(),'job_id':job.name,'status':'completed','ended_at':time.time(),'operation':SCHEMA})
            return 0
        except Exception as error:
            if engine:
                row.update(status='FAILED',error=str(error));engine.state['status']='WAITING'
                if t['kind']=='v2':engine.state['stages']['qa'].update(status='FAILED',error=str(error))
                else:engine.state['qa'].update(status='FAILED',error=str(error))
                engine.persist()
            else:
                p.set_stage(job.name,'claude_vfx','FAILED',str(error));p.set_stage(job.name,'qa','FAILED','Saved-source correction incomplete: '+str(error))
            dump(job/'.source-repair/FAILED.json',{'schema':SCHEMA,'status':'FAILED','error':module.redacted(str(error)),'updated_at':time.time()})
            dump(runtime,{'pid':os.getpid(),'job_id':job.name,'status':'failed','ended_at':time.time(),'operation':SCHEMA})
            emit({'type':'runner','status':'failed','level':'error','text':str(error),'activity':'同任务源修正未完成，真实费用与文件已保留，未自动重试。'})
            return 1

def run_payload(request):
    try:return _run_payload(request)
    except Exception as error:
        import pipeline as p,studio_runner as live
        job=p.job_path(request['job_id']);live._JOB=job
        runtime=job/'.studio-runner.json'
        saved=read(runtime) if runtime.is_file() else {}
        # Only this genuine operation's state can be finalized. Preflight
        # rejections do not manufacture producer attempts or successful work.
        if saved.get('operation')==SCHEMA and saved.get('pid')==os.getpid():
            dump(runtime,{**saved,'status':'failed','ended_at':time.time(),'error':p.redacted(str(error))})
            dump(job/'.source-repair/FAILED.json',{'schema':SCHEMA,'status':'FAILED','error':p.redacted(str(error)),'updated_at':time.time()})
            if (job/'.team-state.json').is_file() and _ACTIVE_ENGINE is not None:
                state=_ACTIVE_ENGINE.state;state['status']='WAITING';state['stages']['qa'].update(status='FAILED',error='Saved-source correction incomplete: '+str(error))
                for member in state['members'].values():
                    if member.get('role')=='vfx' and member.get('status')=='RUNNING':member.update(status='FAILED',error=str(error))
                _ACTIVE_ENGINE.persist()
            elif (job/'.graph-state.json').is_file() and _ACTIVE_ENGINE is not None:
                state=_ACTIVE_ENGINE.state;state['status']='WAITING';state['qa'].update(status='FAILED',error='Saved-source correction incomplete: '+str(error))
                for member in state['members'].values():
                    for row in member.get('runs',{}).values():
                        if row.get('status')=='RUNNING':row.update(status='FAILED',error=str(error))
                _ACTIVE_ENGINE.persist()
            else:
                p.set_stage(job.name,'qa','FAILED','Saved-source correction incomplete: '+str(error))
                if p.stage_rows(job.name)['claude_vfx']['status']=='RUNNING':p.set_stage(job.name,'claude_vfx','FAILED',str(error))
        live.emit({'type':'runner','status':'failed','level':'error','text':p.redacted(str(error)),'activity':'源修正停止；原费用、文件及历史保留，没有自动重试。'})
        return 1

if __name__=='__main__':
    try:sys.exit(run_payload(json.loads(sys.stdin.read())))
    except Exception as error:
        print(json.dumps({'type':'runner','status':'failed','level':'error','text':str(error)},ensure_ascii=False),flush=True);sys.exit(1)
