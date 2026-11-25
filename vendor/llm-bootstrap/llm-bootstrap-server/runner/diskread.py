"""How fast a model's file comes off its disk: one bounded stretch, measured on
demand, and said honestly.

A second read of a stretch is the operating system's cache, not the disk - and
a model that was loaded a minute ago is in that cache whole. A measure that
does not know which it had is not a measure of the disk. So:

  * on Windows the read goes past the cache (`FILE_FLAG_NO_BUFFERING`, into a
    page-aligned buffer, from an offset on a 1 MiB boundary - both multiples
    of any sector), and says so: `past_cache` true;
  * elsewhere it is a plain read, which cannot tell: `past_cache` null - and
    false when this process read the same stretch of the same file before
    with a plain read, because then it is most likely the cache. (Linux's
    O_DIRECT is the seam for a read past the cache there; nothing here needs
    it yet.)

The stretch is a third of the way in - past the header, in the weights a load
reads - at most `STRETCH` bytes, for at most `LIMIT_S` seconds.

Standard library only: the Windows read is ctypes.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

MIB = 1024 * 1024
STRETCH = 64 * MIB
LIMIT_S = 3.0
CHUNK = MIB
#: under this, the sentence says the stretch was too short to say much
ENOUGH = 8 * MIB


def offset_for(size: int) -> int:
    """A third of the way in, on a 1 MiB boundary."""
    return (int(size) // 3) // MIB * MIB


def disk_of(path) -> str | None:
    """The disk a file sits on: its drive or share on Windows, its mount
    point elsewhere; None when that cannot be said."""
    try:
        p = Path(path).resolve()
    except OSError:
        return None
    if sys.platform == "win32":
        drive = os.path.splitdrive(str(p))[0]
        return drive or None
    for q in (p, *p.parents):
        try:
            if os.path.ismount(q):
                return str(q)
        except OSError:
            return None
    return None


def measure(path, *, stretch: int = STRETCH, limit_s: float = LIMIT_S, seen: set | None = None,
            past_cache: bool | None = None, clock=time.perf_counter, now=time.time) -> dict:
    """Read one stretch of *path* and say how fast it came.

    `seen` is the caller's own set of stretches it has read, kept across
    measures; `past_cache` forces the read (True: past the cache, which only
    Windows can; False: a plain read) - None picks the platform's best.
    Raises OSError when the file cannot be read."""
    path = Path(path)
    size = path.stat().st_size
    offset = offset_for(size)
    want = max(0, min(int(stretch), size - offset))
    bypass = (sys.platform == "win32") if past_cache is None else bool(past_cache)
    if bypass and sys.platform != "win32":
        raise ValueError("only Windows can read past the cache here")
    read = _read_past_cache if bypass else _read_plain
    got, seconds, stopped = read(path, offset, want, float(limit_s), clock)
    key = (str(path.resolve()), offset, want, path.stat().st_mtime_ns)
    if bypass:
        past = True
    else:
        past = False if (seen is not None and key in seen) else None
        # only a plain read leaves the stretch in the cache: one past it
        # never put it there
        if seen is not None:
            seen.add(key)
    mb_s = round(got / 1e6 / seconds, 1) if seconds > 0 else 0.0
    return {"bytes": got, "seconds": round(seconds, 4), "mb_s": mb_s, "offset": offset,
            "past_cache": past, "at": float(now()),
            "said": _said(got, seconds, mb_s, past, disk_of(path), stopped, limit_s)}


def _said(got, seconds, mb_s, past, disk, stopped, limit_s) -> str:
    mb = f"{got / 1e6:.0f} MB" if got >= 10e6 else f"{got / 1e6:.1f} MB"
    head = f"{mb} in {seconds:.2f} s: {mb_s:.0f} MB/s"
    where = f"from {disk}" if disk else "from the disk"
    if past is True:
        out = f"{head} {where}, past the operating system's cache"
    elif past is None:
        out = (f"{head} {where} - the operating system may have served it from memory, "
               "so this cannot tell the disk from its cache")
    else:
        out = (f"{head} - this server read that stretch before, so this is most likely the "
               "operating system's cache, not the disk")
    if stopped:
        out += f"; stopped at the {limit_s:g} s limit"
    if got < ENOUGH:
        out += f"; only {mb} - too little to say much about the disk"
    return out


def _read_plain(path, offset, want, limit_s, clock):
    got, stopped = 0, False
    with open(path, "rb", buffering=0) as fh:
        fh.seek(offset)
        start = clock()
        while got < want:
            if clock() - start >= limit_s:
                stopped = True
                break
            chunk = fh.read(min(CHUNK, want - got))
            if not chunk:
                break
            got += len(chunk)
        seconds = clock() - start
    return got, seconds, stopped


def _read_past_cache(path, offset, want, limit_s, clock):
    """Windows: the file opened with FILE_FLAG_NO_BUFFERING, so every read
    goes to the device. Reads are whole chunks - a multiple of any sector -
    into a page-aligned buffer; one past the end of the file returns what
    there is, and a stretch that ends inside a chunk is counted to its end."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.ReadFile.restype = wintypes.BOOL
    k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                     ctypes.c_void_p, wintypes.DWORD]
    k32.SetFilePointerEx.restype = wintypes.BOOL
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
                                 wintypes.DWORD]
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    k32.VirtualFree.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    generic_read = 0x80000000
    share_all = 0x1 | 0x2 | 0x4                     # read, write, delete: nobody is blocked
    open_existing = 3
    no_buffering, sequential = 0x20000000, 0x08000000
    invalid = ctypes.c_void_p(-1).value
    handle = k32.CreateFileW(str(path), generic_read, share_all, None, open_existing,
                             no_buffering | sequential, None)
    if handle is None or handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    buf = None
    try:
        buf = k32.VirtualAlloc(None, CHUNK, 0x1000 | 0x2000, 0x04)   # commit|reserve, rw
        if not buf:
            raise ctypes.WinError(ctypes.get_last_error())
        if not k32.SetFilePointerEx(handle, offset, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        got, stopped = 0, False
        n = wintypes.DWORD(0)
        start = clock()
        while got < want:
            if clock() - start >= limit_s:
                stopped = True
                break
            if not k32.ReadFile(handle, buf, CHUNK, ctypes.byref(n), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if n.value == 0:
                break
            got += n.value
        seconds = clock() - start
        return min(got, want), seconds, stopped
    finally:
        if buf:
            k32.VirtualFree(buf, 0, 0x8000)         # release
        k32.CloseHandle(handle)
