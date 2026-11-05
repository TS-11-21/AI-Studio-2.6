"""File checkpoints of generated job work, excluding credentials and logs."""
from pathlib import Path
import time
import zipfile


def save(p,job,reason='pause'):
    folder=job/'checkpoint'/'versions'
    p.ensure_plain_path(folder); folder.mkdir(parents=True,exist_ok=True)
    target=folder/('checkpoint-'+str(time.time_ns())+'.zip')
    p.ensure_plain_path(target)
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        for workspace in ('game','art','references'):
            for file in (job/workspace).rglob('*'):
                if not file.is_file() or file.is_symlink() or any(part in ('.godot','.git','.pipeline','.mcp','__pycache__') for part in file.parts):
                    continue
                p.ensure_plain_path(file)
                archive.write(file,str(file.relative_to(job)))
        for name in ('task.json','.workers.json','checkpoint/metadata.json'):
            file=job/name
            if file.is_file(): archive.write(file,name)
    versions=sorted(folder.glob('checkpoint-*.zip'),key=lambda file:file.stat().st_mtime,reverse=True)
    for expired in versions[5:]:
        p.ensure_plain_path(expired); expired.unlink()
    return {'path':str(target),'reason':reason,'bytes':target.stat().st_size,'saved':True}
