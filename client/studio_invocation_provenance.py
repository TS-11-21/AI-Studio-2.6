"""Private, host-authenticated billing identity; never a reported-cost record.

An immutable invocation record is created after local preparation and before
dispatch/reservation. Membership usage remains unevaluated in USD. An unsigned
worker file, current default route, or historical filename cannot grant this
exemption. Historical metered/unknown accounting is never rewritten here.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time

SCHEMA='ai-studio-host-invocation/v1'
TAG=re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')
NONMETERED_ADAPTERS={'codex','kimi','external-command','manual-desktop'}

def _plain_stable(p,path):
    # Windows can transiently fail realpath while another member exclusively
    # creates this private file. Repeat the SAME strict path check; never waive
    # redirected-file validation or follow a persistently different target.
    for attempt in range(20):
        try:p.ensure_plain_path(path);return
        except RuntimeError:
            if attempt==19:raise
            time.sleep(.01)

def _payload_bytes(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')

def _paths(p,c,workspace,tag):
    if not isinstance(tag,str) or not TAG.fullmatch(tag):
        raise RuntimeError('Invocation billing identity requires a plain stage tag.')
    workspace=Path(workspace).absolute()
    p.ensure_plain_path(workspace)
    private=Path(c.get('runtime_private_dir',str(p.ROOT/'private'))).absolute()
    p.ensure_plain_path(private)
    store=private/'host-invocations'
    p.ensure_plain_path(store)
    canonical=os.path.normcase(str(workspace))
    identifier=hashlib.sha256((canonical+'\0'+tag).encode('utf-8')).hexdigest()
    proof=store/(identifier+'.json');key=store/'authentication.key'
    _plain_stable(p,proof);_plain_stable(p,key)
    return canonical,store,proof,key

def _key(path,*,create=False):
    if create:
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with path.open('xb') as handle:
                handle.write(os.urandom(32));handle.flush();os.fsync(handle.fileno())
        except FileExistsError:
            pass
    value=path.read_bytes()
    # Independent members may race the one exclusive key-file creator. Wait
    # only for its tiny local write; do not replace a corrupt existing key.
    if create:
        for _ in range(20):
            if len(value)==32:break
            time.sleep(.01);value=path.read_bytes()
    if len(value)!=32:
        raise RuntimeError('The private invocation authentication record is invalid; no worker was dispatched.')
    return value

def _read_verified(proof,key):
    value=json.loads(proof.read_text('utf-8'))
    if not isinstance(value,dict) or set(value)!={'payload','mac'}:
        raise ValueError('Invalid private invocation record.')
    body=value['payload']
    if not isinstance(body,dict) or body.get('schema')!=SCHEMA:
        raise ValueError('Invalid private invocation identity.')
    expected=hmac.new(key,_payload_bytes(body),hashlib.sha256).hexdigest()
    if not isinstance(value['mac'],str) or not hmac.compare_digest(expected,value['mac']):
        raise ValueError('Private invocation identity authentication failed.')
    return body

def record(p,c,workspace,tag,provider):
    """Create once; changing an existing tag's provider/billing is refused."""
    canonical,store,proof,key_path=_paths(p,c,workspace,tag)
    identity={'schema':SCHEMA,'workspace':canonical,'tag':tag,
              'provider_id':provider.provider_id,'adapter':provider.adapter,'billing':provider.billing}
    key=_key(key_path,create=True)
    if proof.is_file():
        previous=_read_verified(proof,key)
        if any(previous.get(k)!=v for k,v in identity.items()):
            raise RuntimeError('Invocation billing identity changed; the historical record is preserved.')
        return
    payload={**identity,'prepared_at':time.time()}
    value={'payload':payload,'mac':hmac.new(key,_payload_bytes(payload),hashlib.sha256).hexdigest()}
    try:
        with proof.open('xb') as handle:
            handle.write(_payload_bytes(value));handle.flush();os.fsync(handle.fileno())
    except FileExistsError:
        previous=_read_verified(proof,key)
        if any(previous.get(k)!=v for k,v in identity.items()):
            raise RuntimeError('Invocation billing identity changed; no worker was dispatched.')

def nonmetered(p,c,workspace,tag):
    """Return true only for a matching authenticated nonmetered host identity.

    Missing, malformed, moved, or tampered records remain unproven; callers
    retain normal cost reconciliation. Merely reading never creates key/files.
    """
    try:
        canonical,store,proof,key_path=_paths(p,c,workspace,tag)
        body=_read_verified(proof,_key(key_path))
        return (body.get('workspace')==canonical and body.get('tag')==tag and
                body.get('adapter') in NONMETERED_ADAPTERS and
                body.get('billing') in ('subscription','manual'))
    except (OSError,ValueError,TypeError,RuntimeError):
        return False
