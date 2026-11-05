"""Windows native process cwd aliases; logical workspaces remain canonical.

This creates no links, changes no registry setting and launches no process.
Only an existing plain directory may use its verified same-directory DOS alias.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat


def _plain_directory(value) -> Path:
    path = Path(value).absolute()
    if not path.is_dir():
        raise RuntimeError('The native working directory does not exist; no process was launched.')
    if path.resolve() != path:
        raise RuntimeError('Redirected working directories are not supported; no process was launched.')
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('Redirected working directories are not supported; no process was launched.')
    return path


def _short_directory(path: Path) -> str:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetShortPathNameW.restype = wintypes.DWORD
    size = kernel.GetShortPathNameW(str(path), None, 0)
    if not 0 < size <= 32768:
        raise RuntimeError('Windows has no usable short working-directory path. Use a shorter data directory; no process was launched.')
    buffer = ctypes.create_unicode_buffer(size)
    count = kernel.GetShortPathNameW(str(path), buffer, size)
    if not 0 < count < size:
        raise RuntimeError('Windows short working-directory lookup failed; no process was launched.')
    return buffer.value


def native_cwd(value) -> str:
    """Return an API-only cwd, verified to denote the unchanged logical directory."""
    path = _plain_directory(value)
    normal = str(path)
    if os.name != 'nt' or len(normal.encode('utf-16-le')) // 2 < 258:
        return normal
    alias = _short_directory(path)
    if len(alias.encode('utf-16-le')) // 2 >= 258:
        raise RuntimeError('Windows has no sufficiently short working-directory alias. Use a shorter data directory; no process was launched.')
    alternative = Path(alias)
    if not alternative.is_absolute() or not alternative.is_dir():
        raise RuntimeError('The native directory alias is invalid; no process was launched.')
    if alternative.resolve() != path or not os.path.samefile(alternative, path):
        raise RuntimeError('The native directory alias is not the same physical workspace; no process was launched.')
    return alias
