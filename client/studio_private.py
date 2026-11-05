"""Private per-Windows-user credential storage; never part of deliveries."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import threading


class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data,decrypt=False):
    if os.name!='nt':
        raise RuntimeError('Remembering keys requires Windows account encryption.')
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    output=Blob()
    if decrypt:
        success=ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output))
    else:
        success=ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source),'AI Studio',None,None,None,1,ctypes.byref(output))
    if not success:
        raise RuntimeError('Windows credential encryption failed; please enter the key again.')
    try:
        return ctypes.string_at(output.data,output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


class SecretStore:
    def __init__(self,path):
        self.path=Path(path)
        self.lock=threading.RLock()
        self.values={}
        self.remembered={}
        self.load_error=False
        if self.path.is_file():
            try:
                self.remembered=json.loads(dpapi(base64.b64decode(self.path.read_bytes()),True))
                self.values.update(self.remembered)
            except Exception:
                self.load_error=True

    def set(self,provider_id,secret,remember=False):
        if not isinstance(provider_id,str) or not provider_id or not isinstance(secret,str) or not secret.strip():
            raise RuntimeError('Enter a provider ID and a nonempty key.')
        with self.lock:
            self.values[provider_id]=secret.strip()
            if remember:
                self.remembered[provider_id]=secret.strip()
            else:
                self.remembered.pop(provider_id,None)
            encrypted=dpapi(json.dumps(self.remembered,ensure_ascii=False).encode('utf-8'))
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.path.with_suffix('.tmp')
            temporary.write_bytes(base64.b64encode(encrypted))
            os.replace(temporary,self.path)

    def snapshot(self):
        with self.lock:
            return dict(self.values)

    def status(self):
        with self.lock:
            return {'providers':{name:{'configured':True,'remembered':name in self.remembered} for name in self.values},
                    'load_error':self.load_error}

    def redact(self,text):
        with self.lock:
            for secret in self.values.values():
                text=text.replace(secret,'[REDACTED]').replace(json.dumps(secret)[1:-1],'[REDACTED]')
        return text
