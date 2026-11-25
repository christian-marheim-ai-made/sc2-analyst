"""THE CHILD MUST NOT SURVIVE ITS PARENT - a kill-on-close Windows job object.

The failure it prevents: a server that is force-killed leaves its
llama-server running - still listening, still holding most of the card - with
nobody left to stop it. `taskkill /F` and `Stop-Process -Force` are
`TerminateProcess`: no `atexit`, no `finally`, no unwinding. The backend's
`stop()` is therefore reached on a GRACEFUL exit and on no other, and nothing
in Windows kills a child because its parent died. Every abnormal exit - a
crash, an out-of-memory, a stray kill - strands the card.

WHY A JOB OBJECT AND NOT CODE. Every other candidate is code that has to RUN in
the dying process - `atexit`, a `finally`, a signal handler, a watchdog - and
the fault is precisely that a terminated process runs nothing. A job object
with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is enforced by the KERNEL when the
LAST HANDLE to the job closes, and the kernel closes a dead process's handles
however it died. It is the one hook a TerminateProcess cannot step over.

TWO WAYS TO GET THIS WRONG THAT LOOK EXACTLY LIKE GETTING IT RIGHT - which is
why the suite kills a real parent and then looks for a real child, instead of
asserting that a handle is not None:

  * AN INHERITABLE JOB HANDLE. If the child held a handle to its own job, the
    parent's death would not close the LAST one and the child would outlive it
    anyway. `CreateJobObjectW(NULL, NULL)` is not inheritable, and the job is
    not created until `CreateProcess` has returned, so the handle does not
    exist when the child could have inherited it. Two independent reasons.
  * A SILENT ASSIGNMENT FAILURE. A job never assigned to looks, from outside,
    like one that was. So nothing here fails quietly: every call is checked
    and raises `JobObjectError` naming the call and `GetLastError`.

POSIX has no job object; a child there is started in its own session.
"""
from __future__ import annotations

import ctypes
import sys

#: kill every process in the job when the last handle to it closes - this one
#: flag is the whole mechanism; the rest of the file is plumbing
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9

#: the narrowest rights that let a process be put in a job, on a handle that
#: lives for one call and is closed in a `finally` straight after
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100

SUPPORTED = sys.platform == "win32"


class JobObjectError(RuntimeError):
    """The child could not be put in a job - so it CAN outlive this process.
    Raised, never swallowed here: the caller decides whether that is fatal."""


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    # ORDER AND WIDTH are the contract here, not the names: the kernel reads
    # this by offset. ctypes' default (unpacked, naturally aligned) layout is
    # MSVC's, so the padding after LimitFlags and ActiveProcessLimit lands
    # where the kernel expects it. A mis-laid struct would set some OTHER
    # limit - which fails exactly like setting none.
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kernel32():
    """kernel32 with its prototypes declared. THE RESTYPES ARE NOT DECORATION:
    ctypes returns `c_int` by default, which TRUNCATES a 64-bit HANDLE - still
    non-zero, so a check passes, and the next call fails on a handle that was
    never ours."""
    from ctypes import wintypes                             # Windows only
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          wintypes.LPVOID, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


class KillOnCloseJob:
    """A job whose members die when this process's handle to it closes.

    Hold the instance for as long as the child should live. Dropping it, or
    `close()`, kills everything in it - which is why a backend closes it only
    AFTER a graceful stop has been confirmed."""

    def __init__(self) -> None:
        if not SUPPORTED:
            raise JobObjectError(f"job objects are a Windows mechanism; this is {sys.platform}")
        self._k = _kernel32()
        self._handle = self._k.CreateJobObjectW(None, None)  # unnamed, NOT inheritable
        if not self._handle:
            raise JobObjectError(f"CreateJobObjectW failed, GetLastError={ctypes.get_last_error()}")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k.SetInformationJobObject(self._handle, JobObjectExtendedLimitInformation,
                                             ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            err = ctypes.get_last_error()
            self._k.CloseHandle(self._handle)
            self._handle = None
            raise JobObjectError(f"SetInformationJobObject(KILL_ON_JOB_CLOSE) failed, "
                                 f"GetLastError={err}. A job without the flag holds the "
                                 f"child exactly as well as no job at all.")

    def add(self, pid: int) -> None:
        """Put *pid* in the job. PID REUSE IS NOT A RISK at the one call site:
        `subprocess.Popen` keeps a handle to the process it started, and
        Windows does not recycle a pid while a handle to it is open."""
        if self._handle is None:
            raise JobObjectError("the job is already closed")
        hproc = self._k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not hproc:
            raise JobObjectError(f"OpenProcess({pid}) failed, GetLastError={ctypes.get_last_error()}")
        try:
            if not self._k.AssignProcessToJobObject(self._handle, hproc):
                raise JobObjectError(f"AssignProcessToJobObject(pid {pid}) failed, "
                                     f"GetLastError={ctypes.get_last_error()}")
        finally:
            self._k.CloseHandle(hproc)

    def close(self) -> None:
        """Release our handle. If it was the last one, the members die NOW."""
        if self._handle is not None:
            self._k.CloseHandle(self._handle)
            self._handle = None

    @property
    def open(self) -> bool:
        return self._handle is not None
