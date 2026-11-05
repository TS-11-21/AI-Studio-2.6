"""Operate only marked, per-job scratch Blender instances. No AI requests."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time


def runtime_directory(studio,c):
    studio=Path(studio).absolute()
    private=studio/'private'
    runtime=Path(c.get('runtime_private_dir',private)).absolute()
    if (studio.resolve()!=studio or runtime.resolve()!=runtime or not runtime.is_relative_to(private)
            or any(part.casefold()=='outputs' for part in runtime.parts)):
        raise RuntimeError('Release runtime files must stay in this data directory\'s private folder, outside the package.')
    return runtime


def rpc(port,code,timeout=12):
    with socket.create_connection(('127.0.0.1',int(port)),timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(json.dumps({'type':'execute_code','params':{'code':code}}).encode())
        data=b''
        while len(data)<4*1024*1024:
            chunk=connection.recv(65536)
            if not chunk:
                break
            data+=chunk
            try:
                response=json.loads(data)
                if response.get('status')!='success':
                    raise RuntimeError('The isolated Blender bridge rejected the operation.')
                return response.get('result',{}).get('result','')
            except json.JSONDecodeError:
                continue
    raise RuntimeError('The isolated Blender bridge did not return a complete response.')


def identity(port):
    code="import bpy, json\nprint(json.dumps({'file':bpy.data.filepath,'isolated':bpy.context.scene.get('ai_studio_isolated_test'), 'job':bpy.context.scene.get('ai_studio_job'), 'version':bpy.app.version_string}))"
    return json.loads(rpc(port,code))


def saved_ack(raw,job_id):
    """Accept the native save's own exact ACK, optionally preceded by its Info line."""
    if not isinstance(raw,str):
        raise RuntimeError('Blender save did not return a textual acknowledgement.')
    try:
        value=json.loads(raw)
    except json.JSONDecodeError:
        lines=[line.strip() for line in raw.splitlines() if line.strip()]
        info={'Info: Saved as "source.blend"','Info: Saved "source.blend"'}
        notices=[line for line in lines if line in info]
        results=[line for line in lines if line not in info]
        if len(notices)!=1 or len(results)!=1:
            raise RuntimeError('Blender save returned unexpected messages; checkpoint is not confirmed.')
        try: value=json.loads(results[0])
        except json.JSONDecodeError as error:
            raise RuntimeError('Blender save returned an invalid acknowledgement.') from error
    if type(value) is not dict or set(value)!={'saved','job'} or value.get('saved') is not True or value.get('job')!=job_id:
        raise RuntimeError('Blender save acknowledgement does not match this isolated job.')
    return value


def save_checkpoint(studio,job_id,port,*,job_dir=None):
    source=(Path(job_dir) if job_dir is not None else Path(studio)/'jobs'/job_id)/'art'/'source.blend'
    if source.resolve()!=source.absolute():
        raise RuntimeError('Redirected Blender checkpoint source is not supported.')
    source=source.resolve()
    if not source.is_relative_to(Path(studio).resolve()/'jobs'):
        raise RuntimeError('Blender checkpoint must belong to this studio member workspace.')
    # The assertions and save happen inside the same Blender operation.
    code=('import bpy, json\n'
          'assert bpy.context.scene.get("ai_studio_isolated_test") is True, "Not an isolated test scene"\n'
          'assert bpy.context.scene.get("ai_studio_job") == '+repr(job_id)+', "Another job is open"\n'
          'assert bpy.data.filepath == '+repr(str(source))+', "Another file is open"\n'
          'bpy.ops.wm.save_as_mainfile(filepath='+repr(str(source))+', check_existing=False)\n'
          'print(json.dumps({"saved":True,"job":'+repr(job_id)+'}))')
    return saved_ack(rpc(port,code,timeout=40),job_id)


def ensure_instance(studio,job_id,c,emit,*,job_dir=None):
    studio=Path(studio)
    runtime=runtime_directory(studio,c)
    job=Path(job_dir) if job_dir is not None else studio/'jobs'/job_id
    if job.resolve()!=job.absolute() or not job.resolve().is_relative_to((studio/'jobs').resolve()):
        raise RuntimeError('Blender must belong to one initialized studio member workspace.')
    source=job/'art'/'source.blend'
    meta=job/'.studio-runtime.json'
    if meta.is_file():
        saved=json.loads(meta.read_text(encoding='utf-8'))
        port=saved.get('blender_port')
        if port:
            try:
                current=identity(port)
                if source.is_file() and current['isolated'] is True and current['job']==job_id and Path(current['file']).resolve()==source.resolve():
                    return int(port)
            except (OSError,ValueError,RuntimeError):
                pass
    existing=int(c.get('blender_port',9877))
    try:
        current=identity(existing)
        if source.is_file() and current['isolated'] is True and current['job']==job_id and Path(current['file']).resolve()==source.resolve():
            return existing
    except (OSError,ValueError,RuntimeError):
        pass
    blender=Path(c.get('blender_path','C:/steam/steamapps/common/Blender/blender.exe'))
    addon=Path(c.get('blender_addon_path',str(Path(os.environ.get('APPDATA',''))/'Blender Foundation'/'Blender'/'5.2'/'scripts'/'addons'/'blender_mcp.py')))
    if not blender.is_file() or not addon.is_file():
        raise RuntimeError('Blender or its MCP add-on is missing; other AI roles may continue.')
    with socket.socket() as free:
        free.bind(('127.0.0.1',0)); port=free.getsockname()[1]
    profile=runtime/'blender-profiles'/str(port)
    env=dict(os.environ)
    for variable,subdirectory in [('BLENDER_USER_CONFIG','config'),('BLENDER_USER_SCRIPTS','scripts'),('BLENDER_USER_EXTENSIONS','extensions'),('BLENDER_USER_DATAFILES','datafiles')]:
        path=profile/subdirectory; path.mkdir(parents=True,exist_ok=True); env[variable]=str(path)
    env.update(AI_STUDIO_ROOT=str(studio),AI_STUDIO_BLENDER_JOB=job_id,AI_STUDIO_BLENDER_PORT=str(port),
               AI_STUDIO_BLENDER_ADDON=str(addon),AI_STUDIO_JOB_PATH=str(job),AI_STUDIO_RUNTIME_PRIVATE_DIR=str(runtime),BLENDERMCP_NO_UPDATE_CHECK='1')
    args=[str(blender),'--factory-startup','--no-window-focus']
    if source.is_file(): args.append(str(source))
    args+=['--python',str(studio/'tools'/'blender-isolated'/'bootstrap.py')]
    runtime.mkdir(parents=True,exist_ok=True)
    with (runtime/('blender-'+job_id+'.log')).open('ab') as log:
        process=subprocess.Popen(args,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    emit({'type':'activity','stage':'claude_vfx','activity':'正在启动该任务的独立 Blender 场景','level':'info'})
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError('The new scratch Blender instance exited; existing scenes were not changed.')
        try:
            current=identity(port)
            if source.is_file() and current['isolated'] is True and current['job']==job_id and Path(current['file']).resolve()==source.resolve():
                data={'blender_port':port,'blender_pid':process.pid,'job_id':job_id}
                temporary=meta.with_suffix('.tmp'); temporary.write_text(json.dumps(data),encoding='utf-8'); os.replace(temporary,meta)
                return port
        except (OSError,ValueError,RuntimeError):
            pass
        time.sleep(.5)
    raise RuntimeError('The new scratch Blender instance did not become ready; other roles can continue.')
