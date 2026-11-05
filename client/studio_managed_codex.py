"""Resolve only a disappeared OpenAI-managed Codex executable, without config edits.

Existing/custom commands and every argument are untouched. No PATH lookup, model
request, authentication change, or provider fallback is performed.
"""
from __future__ import annotations
from dataclasses import dataclass
import ctypes
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading

HASH_FOLDER=re.compile(r'[0-9a-fA-F]{16}\Z')
PUBLISHERS={'OpenAI OpCo, LLC','OpenAI OpCo'}
_ANNOUNCED=set()
_ANNOUNCE_LOCK=threading.Lock()

@dataclass(frozen=True)
class Resolution:
    command: tuple[str,...]
    relocated: bool
    original_path: str
    selected_path: str
    signer: str|None=None

def _plain(path):
    path=Path(path)
    for item in [path,*path.parents]:
        try: info=item.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode)or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
            raise RuntimeError('Managed Codex relocation refuses redirected paths.')
    if path.resolve()!=path.absolute():
        raise RuntimeError('Managed Codex relocation refuses noncanonical paths.')

def _approved(signature):
    return (isinstance(signature,dict)and signature.get('status')=='Valid'and signature.get('signer')in PUBLISHERS
            and isinstance(signature.get('organizations'),list)and len(signature['organizations'])==1
            and signature['organizations'][0]in PUBLISHERS)

def _host_spawn():
    # Signature inspection is fixed host maintenance, not a model dispatch.
    # Match only this application's active hook; never bypass it for workers.
    spawn=subprocess.Popen
    adapter=sys.modules.get('adapter_runtime')
    if adapter is not None:
        with adapter._DISPATCH_LOCK:
            for hooks in adapter._DISPATCH_HOOKS.values():
                if spawn is hooks['observed_spawn']:return hooks['spawn']
    return spawn

def _authenticode(path):
    if os.name!='nt':raise RuntimeError('Managed Codex Authenticode inspection requires Windows.')
    buffer=ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer,len(buffer)):
        raise RuntimeError('The trusted Windows signature utility is unavailable.')
    powershell=Path(buffer.value)/'System32/WindowsPowerShell/v1.0/powershell.exe'
    _plain(powershell)
    if not powershell.is_file():raise RuntimeError('The trusted Windows signature utility is unavailable.')
    script='''$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
Import-Module -Name ($PSHOME+'\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') -Force
Import-Module -Name ($PSHOME+'\\Modules\\Microsoft.PowerShell.Utility\\Microsoft.PowerShell.Utility.psd1') -Force
$s=Microsoft.PowerShell.Security\\Get-AuthenticodeSignature -LiteralPath $env:AI_STUDIO_MANAGED_SIGNATURE_PATH
$signer=$null; $org=@()
if($s.SignerCertificate) {
 $signer=$s.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false)
 $decoded=$s.SignerCertificate.SubjectName.Decode([System.Security.Cryptography.X509Certificates.X500DistinguishedNameFlags]::UseNewLines)
 $org=@($decoded -split '\\r?\\n' | ForEach-Object { if($_ -match '^O=(.*)$') { $Matches[1].Trim().Trim('"') } })
}
@{status=[string]$s.Status;signer=$signer;organizations=$org} | Microsoft.PowerShell.Utility\\ConvertTo-Json -Compress
'''
    environment={k:v for k,v in os.environ.items()if not any(s in k.upper()for s in ('TOKEN','SECRET','PASSWORD','CREDENTIAL','API_KEY'))}
    environment['AI_STUDIO_MANAGED_SIGNATURE_PATH']=str(path)
    environment.pop('PSModulePath',None)
    process=_host_spawn()([str(powershell),'-NoProfile','-NonInteractive','-Command',script],
        env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:out,err=process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill();process.communicate(timeout=5)
        raise RuntimeError('Managed Codex signature inspection timed out.') from None
    if process.returncode:raise RuntimeError('Managed Codex signature inspection failed; no executable was selected.')
    try:return json.loads(out.decode('utf-8-sig'))
    except (ValueError,UnicodeError):raise RuntimeError('Managed Codex signature result is invalid.') from None

def _resolve(command,user_profile,verifier):
    if not isinstance(command,(list,tuple))or not command or not all(isinstance(x,str)and x for x in command):
        raise RuntimeError('Codex command must be a nonempty argument array.')
    original=tuple(command);first=Path(command[0])
    unchanged=Resolution(original,False,command[0],command[0])
    if not first.is_absolute():return unchanged
    try:first.stat();return unchanged
    except FileNotFoundError:pass
    except OSError:raise RuntimeError('Cannot determine whether the configured Codex executable is missing.') from None
    if not user_profile:return unchanged
    managed=Path(user_profile)/'AppData/Local/OpenAI/Codex/bin'
    if not managed.is_absolute():return unchanged
    try:relative=first.relative_to(managed)
    except ValueError:return unchanged
    if len(relative.parts)!=2 or not HASH_FOLDER.fullmatch(relative.parts[0])or relative.parts[1].casefold()!='codex.exe':
        return unchanged
    _plain(first);_plain(managed)
    if not managed.is_dir():raise RuntimeError('The configured managed Codex installation is unavailable.')
    accepted=[]
    for folder in managed.iterdir():
        if not HASH_FOLDER.fullmatch(folder.name):continue
        _plain(folder)
        if not folder.is_dir():continue
        path=folder/'codex.exe';_plain(path)
        try:before=path.stat()
        except FileNotFoundError:continue
        if not stat.S_ISREG(before.st_mode):continue
        signature=verifier(path)
        after=path.stat();_plain(path)
        if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
            raise RuntimeError('A managed Codex candidate changed during signature inspection.')
        if _approved(signature):accepted.append((path,signature['signer']))
    if len(accepted)!=1:
        raise RuntimeError('Expired managed Codex path has no unique OpenAI-signed replacement; select an explicit valid command.')
    selected,signer=accepted[0]
    return Resolution((str(selected),*original[1:]),True,command[0],str(selected),signer)

def resolve_command(command):
    resolution=_resolve(command,os.environ.get('USERPROFILE'),_authenticode)
    if resolution.relocated:
        pair=(resolution.original_path,resolution.selected_path)
        with _ANNOUNCE_LOCK:
            if pair not in _ANNOUNCED:
                logging.getLogger('ai_studio.managed_codex').warning('Expired managed Codex executable relocated: %s -> %s; original arguments and configuration remain unchanged.',*pair)
                _ANNOUNCED.add(pair)
    return list(resolution.command)
