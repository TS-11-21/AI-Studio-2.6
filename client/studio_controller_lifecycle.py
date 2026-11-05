"""Local controller ownership. Window closure leaves the owner alive.

Runners retain an OS process handle, detect their original controller's exit,
and request a persisted pause. This never calls a model or changes accounting.
"""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
import os
import threading

_ACTIVE_GUARD=None

def _windows():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.GetProcessTimes.argtypes=[ctypes.c_void_p]+[ctypes.c_void_p]*4
    kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
    kernel.WaitForSingleObject.restype=ctypes.c_ulong
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    return kernel

def _created(kernel,handle):
    values=[ctypes.c_ulonglong() for _ in range(4)]
    if not kernel.GetProcessTimes(handle,*[ctypes.byref(v) for v in values]):
        raise OSError(ctypes.get_last_error(),'Cannot verify controller process identity.')
    return str(values[0].value)

def owner_identity():
    identity={'pid':os.getpid(),'policy':'pause-on-controller-exit'}
    if os.name=='nt':
        kernel=_windows();handle=kernel.OpenProcess(0x101000,False,os.getpid())
        if not handle:raise OSError(ctypes.get_last_error(),'Cannot bind controller identity.')
        try:identity['created']=_created(kernel,handle)
        finally:kernel.CloseHandle(handle)
    else:
        identity['created']=str(os.stat('/proc/'+str(os.getpid())).st_ctime_ns) if os.path.exists('/proc/'+str(os.getpid())) else None
    return identity

class ControllerGuard:
    def __init__(self,identity,on_lost,interval=.5):
        self.identity=identity;self.on_lost=on_lost;self.interval=interval
        self.lost=threading.Event();self.stop=threading.Event();self.lock=threading.RLock()
        self.kernel=None;self.handle=None;self.thread=None;self.reason=None
        self.valid=isinstance(identity,dict) and isinstance(identity.get('pid'),int) and not isinstance(identity.get('pid'),bool) and identity['pid']>0 and identity.get('policy')=='pause-on-controller-exit'
        if self.valid and os.name=='nt':
            self.kernel=_windows();self.handle=self.kernel.OpenProcess(0x101000,False,identity['pid'])
            if not self.handle:
                self.valid=False;self.reason='Controller ownership cannot be verified; pause conservatively.'
            else:
                try:self.valid=_created(self.kernel,self.handle)==identity.get('created')
                except OSError:self.valid=False
        try:self.refresh()
        except Exception:
            if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None
            raise
        self.thread=threading.Thread(target=self._watch,name='controller-owner-watch',daemon=True);self.thread.start()

    def _alive(self):
        if not self.valid:return False
        if os.name=='nt':return self.kernel.WaitForSingleObject(self.handle,0)==258
        try:
            os.kill(self.identity['pid'],0)
            if self.identity.get('created') is not None:
                return str(os.stat('/proc/'+str(self.identity['pid'])).st_ctime_ns)==self.identity['created']
            return True
        except OSError:return False

    def refresh(self):
        with self.lock:
            if not self.lost.is_set() and not self._alive():
                self.lost.set();self.reason=self.reason or 'The original controller exited; pause and retain task progress.'
            if self.lost.is_set():
                # Resume can remove a previous pause marker. A lost owner is
                # latched for this runner and must reassert its own pause.
                self.on_lost(self.reason)
            return not self.lost.is_set()

    def _watch(self):
        while not self.stop.wait(self.interval):
            try:
                if not self.refresh():return
            except OSError:pass

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=2)
        with self.lock:
            if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None

def check_dispatch():
    guard=_ACTIVE_GUARD
    if guard is not None and not guard.refresh():
        raise RuntimeError('Controller exited; no new worker or API call is dispatched. Progress is being paused.')

@contextmanager
def controller_owned(request,on_lost):
    global _ACTIVE_GUARD
    identity=request.get('controller_owner')
    guard=ControllerGuard(identity,on_lost) if identity is not None else None
    previous=_ACTIVE_GUARD;_ACTIVE_GUARD=guard
    try:yield guard
    finally:
        _ACTIVE_GUARD=previous
        if guard:guard.close()

def guarded_recovery(guard,recover):
    def call(*args,**kwargs):
        if guard is not None and not guard.refresh():return None
        result=recover(*args,**kwargs)
        if guard is not None:guard.refresh()
        return result
    return call
