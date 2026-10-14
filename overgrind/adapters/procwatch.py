"""psutil-free process liveness / hang / tree-kill helpers.

Windows paths use ctypes (imported lazily, never at module import). POSIX fallbacks exist so the
watchdog and tests behave on the Linux dev box. Nothing here sends input to a window.
"""
from __future__ import annotations

import os
import shutil
import signal
import struct
import subprocess
import sys
import time

_WIN = sys.platform == "win32"
STILL_ACTIVE = 259


# ------------------------------------------------------------------ ctypes plumbing (Windows)
# Every foreign function gets explicit argtypes/restype: on 64-bit Windows the ctypes default
# (c_int) truncates HANDLE/HWND/HDC/LPARAM values, which silently corrupts pointers. The DLLs are
# loaded with WinDLL(use_last_error=True) so ctypes.get_last_error() is reliable (it is a private
# copy of GetLastError captured right after each call). Built lazily so Linux import never touches
# ctypes.wintypes' Windows-only DLL handling.
ERROR_ACCESS_DENIED = 5
ERROR_TIMEOUT = 1460
SMTO_BLOCK, SMTO_ABORTIFHUNG = 0x0001, 0x0002
PROCESS_TERMINATE, PROCESS_SET_QUOTA = 0x0001, 0x0100
PROCESS_QUERY_INFORMATION, PROCESS_QUERY_LIMITED_INFORMATION = 0x0400, 0x1000
_api_cache: dict = {}


def _api() -> dict:
    """Lazily-built namespace of typed Win32 entry points (Windows only)."""
    if _api_cache:
        return _api_cache
    import ctypes
    from ctypes import wintypes as wt
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    V = ctypes.c_void_p

    def sig(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)

    sig(k.OpenProcess, wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    sig(k.CloseHandle, wt.BOOL, wt.HANDLE)
    sig(k.GetExitCodeProcess, wt.BOOL, wt.HANDLE, ctypes.POINTER(wt.DWORD))
    sig(k.TerminateProcess, wt.BOOL, wt.HANDLE, wt.UINT)
    sig(k.GetCurrentProcess, wt.HANDLE)
    sig(k.CreateToolhelp32Snapshot, wt.HANDLE, wt.DWORD, wt.DWORD)
    sig(k.CreateJobObjectW, wt.HANDLE, V, wt.LPCWSTR)
    sig(k.SetInformationJobObject, wt.BOOL, wt.HANDLE, ctypes.c_int, V, wt.DWORD)
    sig(k.AssignProcessToJobObject, wt.BOOL, wt.HANDLE, wt.HANDLE)
    sig(a.OpenProcessToken, wt.BOOL, wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE))
    sig(a.GetTokenInformation, wt.BOOL, wt.HANDLE, ctypes.c_int, V, wt.DWORD, ctypes.POINTER(wt.DWORD))
    sig(u.EnumWindows, wt.BOOL, V, wt.LPARAM)          # callback passed as an opaque function pointer
    sig(u.GetWindowThreadProcessId, wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
    sig(u.IsWindowVisible, wt.BOOL, wt.HWND)
    sig(u.IsWindowEnabled, wt.BOOL, wt.HWND)
    sig(u.IsWindow, wt.BOOL, wt.HWND)
    sig(u.IsHungAppWindow, wt.BOOL, wt.HWND)
    sig(u.GetWindow, wt.HWND, wt.HWND, wt.UINT)
    sig(u.GetClassNameW, ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    sig(u.GetWindowTextW, ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    sig(u.GetForegroundWindow, wt.HWND)
    sig(u.GetWindowRect, wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
    sig(u.GetWindowDC, wt.HDC, wt.HWND)
    sig(u.ReleaseDC, ctypes.c_int, wt.HWND, wt.HDC)
    sig(u.PrintWindow, wt.BOOL, wt.HWND, wt.HDC, wt.UINT)
    sig(u.SendMessageTimeoutW, wt.LPARAM, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
        ctypes.POINTER(ctypes.c_size_t))
    sig(g.CreateCompatibleDC, wt.HDC, wt.HDC)
    sig(g.CreateCompatibleBitmap, wt.HBITMAP, wt.HDC, ctypes.c_int, ctypes.c_int)
    sig(g.SelectObject, wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
    sig(g.DeleteObject, wt.BOOL, wt.HGDIOBJ)
    sig(g.DeleteDC, wt.BOOL, wt.HDC)
    sig(g.BitBlt, wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HDC,
        ctypes.c_int, ctypes.c_int, wt.DWORD)
    sig(u.GetDC, wt.HDC, wt.HWND)
    sig(g.GetDIBits, ctypes.c_int, wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, V, V, wt.UINT)
    sh = None
    try:
        sh = ctypes.WinDLL("shcore", use_last_error=True)
    except OSError:
        pass
    _api_cache.update(u=u, k=k, a=a, g=g, sh=sh, ct=ctypes, wt=wt)
    return _api_cache


def set_dpi_aware() -> bool:
    """Make THIS process PER_MONITOR_AWARE_V2 DPI aware. Call before any UIA bounds / window
    geometry is read (UIA rectangles and GetWindowRect are DPI-virtualised for unaware processes,
    so screenshots and bounds would be wrong). Idempotent; False if it was already set / failed."""
    if not _WIN:
        return False
    api = _api()
    ct, u = api["ct"], api["u"]
    try:                                    # Win10 1703+: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        fn = u.SetProcessDpiAwarenessContext
        fn.restype, fn.argtypes = ct.c_int, [ct.c_void_p]
        if fn(ct.c_void_p(-4)):
            return True
    except (AttributeError, OSError):
        pass
    try:                                    # Win8.1: PROCESS_PER_MONITOR_DPI_AWARE == 2
        if api["sh"] is not None:
            api["sh"].SetProcessDpiAwareness.argtypes = [ct.c_int]
            api["sh"].SetProcessDpiAwareness.restype = ct.c_long
            if api["sh"].SetProcessDpiAwareness(2) == 0:
                return True
    except (AttributeError, OSError):
        pass
    try:
        return bool(u.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


# ------------------------------------------------------------------ liveness
def _open(pid: int, access: int):
    api = _api()
    api["ct"].set_last_error(0)
    return api["k"].OpenProcess(access, False, int(pid))


def is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if _WIN:
        api = _api()
        ct, k = api["ct"], api["k"]
        h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
        if not h:
            # ACCESS_DENIED => the process exists but is more privileged; INVALID_PARAMETER => gone.
            return ct.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = api["wt"].DWORD()
            if not k.GetExitCodeProcess(h, ct.byref(code)):
                return False
            # A process that really exited with code 259 would look alive; accepted (documented caveat).
            return code.value == STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            data = fh.read()
        state = data[data.rindex(b")") + 2:data.rindex(b")") + 3]
        return state not in (b"Z", b"X")          # zombie == dead
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        pass
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def exit_code(pid: int) -> int | None:
    """Exit code of a finished process we can still query (Windows only), else None."""
    if not _WIN:
        return None
    api = _api()
    h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return None
    try:
        code = api["wt"].DWORD()
        if api["k"].GetExitCodeProcess(h, api["ct"].byref(code)) and code.value != STILL_ACTIVE:
            return int(code.value)
        return None
    finally:
        api["k"].CloseHandle(h)


# ------------------------------------------------------------------ process tree (Windows)
def descendant_pids(pid: int) -> list[int]:
    """All transitive children of pid from a Toolhelp snapshot (Windows only; [] elsewhere).
    Caveat: parent links are not cleaned up when a parent exits, so PID reuse can in theory add a
    stranger; callers only use this for the target's own windows / cleanup."""
    if not _WIN:
        return list(_descendants_posix(int(pid)))
    api = _api()
    ct, wt, k = api["ct"], api["wt"], api["k"]

    class PE32(ct.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ct.c_size_t), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]

    k.Process32FirstW.restype = k.Process32NextW.restype = wt.BOOL
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wt.HANDLE, ct.POINTER(PE32)]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)                  # TH32CS_SNAPPROCESS
    if not snap or snap == ct.c_void_p(-1).value:               # INVALID_HANDLE_VALUE
        return []
    kids: dict[int, list[int]] = {}
    try:
        pe = PE32()
        pe.dwSize = ct.sizeof(PE32)
        ok = k.Process32FirstW(snap, ct.byref(pe))
        while ok:
            kids.setdefault(int(pe.th32ParentProcessID), []).append(int(pe.th32ProcessID))
            ok = k.Process32NextW(snap, ct.byref(pe))
    finally:
        k.CloseHandle(snap)
    out: list[int] = []
    stack = [int(pid)]
    while stack:
        for c in kids.get(stack.pop(), ()):
            if c != int(pid) and c not in out:
                out.append(c)
                stack.append(c)
    return out


# ------------------------------------------------------------------ windows of a pid
def top_windows(pid: int, tree: bool = False) -> list[int]:
    """Top-level HWNDs (visible or not) owned by pid - or, with tree=True, by pid and all its
    descendant processes (Electron/Chromium style multi-process apps, launcher shims)."""
    if not _WIN:
        return []
    api = _api()
    ct, wt, u = api["ct"], api["wt"], api["u"]
    pids = {int(pid)} | (set(descendant_pids(pid)) if tree else set())
    found: list[int] = []
    # NOTE: UWP apps put their visible frame in an ApplicationFrameHost.exe window; that is not in
    # the target's process tree and is not found here (known limitation, see docs/audit).
    proc = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _lp):
        p = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ct.byref(p))
        if p.value in pids and hwnd:
            found.append(int(hwnd))
        return True                          # keep enumerating

    fn = proc(cb)                            # keep a reference alive for the duration of the call
    u.EnumWindows(ct.cast(fn, ct.c_void_p), 0)
    return found


def window_class(hwnd: int) -> str:
    if not _WIN:
        return ""
    api = _api()
    buf = api["ct"].create_unicode_buffer(256)
    api["u"].GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_title(hwnd: int) -> str:
    if not _WIN:
        return ""
    api = _api()
    buf = api["ct"].create_unicode_buffer(512)
    api["u"].GetWindowTextW(hwnd, buf, 512)
    return buf.value


def modal_dialogs(pid: int, main_hwnd: int | None = None) -> list[tuple[int, str]]:
    """Visible dialogs of the pid tree as (hwnd, title): '#32770' windows, or any visible owned
    window while its owner (the main window) is disabled (WS_DISABLED) - which is how Qt/WPF/
    WinForms/MessageBox modal boxes look. Non-modal owned tool windows are not reported."""
    if not _WIN:
        return []
    u = _api()["u"]
    out = []
    for h in top_windows(pid, tree=True):
        if h == main_hwnd or not u.IsWindowVisible(h):
            continue
        owner = u.GetWindow(h, 4)                        # GW_OWNER
        if window_class(h) == "#32770" or (owner and not u.IsWindowEnabled(owner)):
            out.append((h, window_title(h)))
    return out


def window_exists(hwnd: int) -> bool:
    return True if not _WIN else bool(_api()["u"].IsWindow(hwnd))


def foreground_pid() -> int | None:
    if not _WIN:
        return None
    api = _api()
    h = api["u"].GetForegroundWindow()
    if not h:
        return None
    p = api["wt"].DWORD()
    api["u"].GetWindowThreadProcessId(h, api["ct"].byref(p))
    return int(p.value)


# ------------------------------------------------------------------ hang detection
def is_hung(target: int, pid: bool = False, timeout_ms: int = 1000) -> bool:
    """True if the window (hwnd) - or, with pid=True, any visible top window of that pid tree -
    is not pumping messages: IsHungAppWindow, or SendMessageTimeoutW(WM_NULL,
    SMTO_ABORTIFHUNG|SMTO_BLOCK). A 0 return is a hang only when GetLastError is ERROR_TIMEOUT
    (or 0, which is what SMTO_ABORTIFHUNG short-circuit reports); other errors (invalid window,
    access denied across UIPI) mean "cannot tell", not "hung"."""
    if not _WIN:
        return False
    api = _api()
    ct, u = api["ct"], api["u"]
    hwnds = top_windows(target, tree=True) if pid else [target]
    for hwnd in hwnds:
        if pid and not u.IsWindowVisible(hwnd):
            continue
        if u.IsHungAppWindow(hwnd):
            return True
        res = ct.c_size_t()
        ct.set_last_error(0)
        ok = u.SendMessageTimeoutW(hwnd, 0, 0, 0, SMTO_ABORTIFHUNG | SMTO_BLOCK, int(timeout_ms),
                                   ct.byref(res))
        if not ok and ct.get_last_error() in (0, ERROR_TIMEOUT):
            return True
    return False


# ------------------------------------------------------------------ elevation / integrity
def is_elevated(pid: int | None = None) -> bool | None:
    """Is the process (default: this one) elevated? None if unknown / not Windows."""
    if not _WIN:
        return None
    api = _api()
    ct, wt, k, adv = api["ct"], api["wt"], api["k"], api["a"]
    own = pid is None
    h = k.GetCurrentProcess() if own else (_open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
                                           or _open(pid, PROCESS_QUERY_INFORMATION))
    if not h:
        return None            # access denied typically means the target is more privileged
    tok = wt.HANDLE()
    try:
        if not adv.OpenProcessToken(h, 0x0008, ct.byref(tok)):   # TOKEN_QUERY
            return None
        elev = wt.DWORD()                                        # TOKEN_ELEVATION { DWORD TokenIsElevated }
        size = wt.DWORD()
        if not adv.GetTokenInformation(tok, 20, ct.byref(elev), ct.sizeof(elev),
                                       ct.byref(size)):          # TokenElevation
            return None
        return bool(elev.value)
    finally:
        if tok:
            k.CloseHandle(tok)
        if not own:
            k.CloseHandle(h)


# ------------------------------------------------------------------ launch + job object
class Job:
    """Windows Job Object with KILL_ON_JOB_CLOSE: if the owner (worker) dies the whole target tree
    dies with it. `release()` clears the flag so a deliberate detach(kill=False) leaves the target
    running. Nested jobs are supported on Windows 8+, so assignment normally succeeds even when
    Python itself runs inside a job (CI runners, Windows Terminal, PyInstaller bootloader); when it
    fails (older OS, job with restrictive limits) we return None and rely on taskkill /T."""

    def __init__(self) -> None:
        self.handle = None

    @classmethod
    def create(cls, kill_on_close: bool = True) -> "Job | None":
        if not _WIN:
            return None
        api = _api()
        ct, wt, k = api["ct"], api["wt"], api["k"]

        class BASIC(ct.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ct.c_int64), ("PerJobUserTimeLimit", ct.c_int64),
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ct.c_size_t),
                        ("MaximumWorkingSetSize", ct.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ct.c_size_t), ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]

        class EXT(ct.Structure):
            _fields_ = [("Basic", BASIC), ("IoInfo", ct.c_uint64 * 6), ("ProcessMemoryLimit", ct.c_size_t),
                        ("JobMemoryLimit", ct.c_size_t), ("PeakProcessMemoryUsed", ct.c_size_t),
                        ("PeakJobMemoryUsed", ct.c_size_t)]

        h = k.CreateJobObjectW(None, None)
        if not h:
            return None
        job = cls()
        job.handle, job._EXT = h, EXT
        if not job._set_flags(0x2000 if kill_on_close else 0):  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            job.close(kill=False)
            return None
        return job

    def _set_flags(self, flags: int) -> bool:
        api = _api()
        info = self._EXT()
        info.Basic.LimitFlags = flags
        return bool(api["k"].SetInformationJobObject(self.handle, 9, api["ct"].byref(info),
                                                     api["ct"].sizeof(info)))   # JobObjectExtendedLimitInformation

    def assign(self, pid: int) -> bool:
        api = _api()
        h = _open(pid, PROCESS_SET_QUOTA | PROCESS_TERMINATE)
        if not h:
            return False
        try:
            return bool(api["k"].AssignProcessToJobObject(self.handle, h))
        finally:
            api["k"].CloseHandle(h)

    def release(self) -> None:
        """Stop killing on close, then drop the handle (target keeps running)."""
        if self.handle:
            self._set_flags(0)
            self.close(kill=False)

    def close(self, kill: bool = True) -> None:
        if self.handle:
            _api()["k"].CloseHandle(self.handle)     # with KILL_ON_JOB_CLOSE still set this kills the tree
            self.handle = None


_jobs: dict[int, Job] = {}
_procs: dict[int, subprocess.Popen] = {}


def launch(args: list[str], cwd: str | None = None) -> int:
    """Start a GUI target and return its pid. Own process group (a Ctrl+C aimed at us is not
    delivered to it), no inherited handles (std handles = NUL, close_fds), no console window,
    best-effort kill-on-close Job Object. We deliberately do not use CREATE_SUSPENDED: a child the
    target spawns in the few ms before assignment would escape the job (taskkill /T still covers
    it)."""
    flags = 0
    if _WIN:
        flags = 0x00000200 | 0x08000000          # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    proc = subprocess.Popen(list(args), cwd=cwd, creationflags=flags, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _procs[proc.pid] = proc
    if _WIN:
        try:
            job = Job.create()
            if job is not None:
                if job.assign(proc.pid):
                    _jobs[proc.pid] = job
                else:
                    job.close(kill=False)
        except Exception:
            pass
    return proc.pid


def release_job(pid: int) -> None:
    """detach(kill=False): leave the target running even if we exit."""
    job = _jobs.pop(int(pid), None)
    if job is not None:
        job.release()
    _procs.pop(int(pid), None)


def _terminate(pid: int) -> None:
    if not _WIN:
        return
    api = _api()
    h = _open(pid, PROCESS_TERMINATE)
    if h:
        try:
            api["k"].TerminateProcess(h, 1)
        finally:
            api["k"].CloseHandle(h)


# ------------------------------------------------------------------ process tree kill
def _children_posix() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return kids
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(f"/proc/{n}/stat", "rb") as fh:
                d = fh.read()
            ppid = int(d[d.rindex(b")") + 2:].split()[1])
            kids.setdefault(ppid, []).append(int(n))
        except (OSError, ValueError, IndexError):
            continue
    return kids


def _descendants_posix(pid: int) -> list[int]:
    kids = _children_posix()
    out: list[int] = []
    stack = [pid]
    while stack:
        for c in kids.get(stack.pop(), ()):
            out.append(c)
            stack.append(c)
    return out


def kill_tree(pid: int | None) -> bool:
    """Kill pid and all descendants. Returns True if the root is gone afterwards."""
    if not pid or pid <= 0 or pid == os.getpid():
        return False
    if _WIN:
        # Snapshot the tree first: once the root dies, `taskkill /T` can no longer find its children.
        victims = [int(pid)] + descendant_pids(int(pid))
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))], capture_output=True,
                           timeout=10, creationflags=0x08000000)
        except (OSError, subprocess.SubprocessError):
            pass
        for v in victims:                      # belt and braces: taskkill missing / access quirks
            if is_alive(v):
                _terminate(v)
        job = _jobs.pop(int(pid), None)
        if job is not None:
            job.close()                        # KILL_ON_JOB_CLOSE reaps anything that escaped
        proc = _procs.pop(int(pid), None)
        if proc is not None:
            try:
                proc.wait(timeout=2)           # reap our Popen handle
            except Exception:
                pass
    else:
        kids = _children_posix()
        order: list[int] = []
        stack = [int(pid)]
        while stack:
            p = stack.pop()
            order.append(p)
            stack.extend(kids.get(p, ()))
        for p in reversed(order):
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.02)
    return not is_alive(pid)


# ------------------------------------------------------------------ crash dump (optional)
def write_minidump(pid: int, path: str, timeout: float = 30.0) -> bool:
    """Full dump via Sysinternals ProcDump when it is on PATH. Never required."""
    exe = shutil.which("procdump64") or shutil.which("procdump")
    if not exe:
        return False
    try:
        subprocess.run([exe, "-accepteula", "-ma", str(int(pid)), path], capture_output=True,
                       timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.isfile(path)


# ------------------------------------------------------------------ screenshot (no PIL)
def print_window_bmp(hwnd: int, path: str) -> bool:
    """PrintWindow(PW_RENDERFULLCONTENT) into a bottom-up 24-bit BI_RGB BMP via ctypes (Windows
    only). Rows are padded to 4-byte stride; positive biHeight == bottom-up, which is what
    GetDIBits returns for that request and what BMP viewers expect."""
    if not _WIN:
        return False
    api = _api()
    ct, wt, u, g = api["ct"], api["wt"], api["u"], api["g"]
    rect = wt.RECT()
    if not u.GetWindowRect(hwnd, ct.byref(rect)):
        return False
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0 or w * h > 64_000_000:
        return False
    hdc = u.GetWindowDC(hwnd)
    if not hdc:
        return False
    mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    if not (mdc and bmp):
        if bmp:
            g.DeleteObject(bmp)
        if mdc:
            g.DeleteDC(mdc)
        u.ReleaseDC(hwnd, hdc)
        return False
    old = g.SelectObject(mdc, bmp)
    try:
        if is_hung(hwnd, timeout_ms=200):
            # PrintWindow sends WM_PRINT synchronously and would block on a hung window: copy what
            # is on screen instead (shows the ghost/"not responding" window).
            sdc = u.GetDC(None)
            try:
                if not sdc or not g.BitBlt(mdc, 0, 0, w, h, sdc, rect.left, rect.top, 0x00CC0020):  # SRCCOPY
                    return False
            finally:
                if sdc:
                    u.ReleaseDC(None, sdc)
        elif not u.PrintWindow(hwnd, mdc, 2):    # PW_RENDERFULLCONTENT (Win 8.1+)
            return False
        g.SelectObject(mdc, old)                 # GetDIBits requires the bitmap NOT be selected into a DC
        old = None

        class BIH(ct.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                        ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        stride = (w * 3 + 3) & ~3
        bih = BIH(ct.sizeof(BIH), w, h, 1, 24, 0, stride * h, 0, 0, 0, 0)
        buf = ct.create_string_buffer(stride * h)
        # DIB_RGB_COLORS == 0; BITMAPINFO for 24bpp is just the header (no palette)
        if not g.GetDIBits(mdc, bmp, 0, h, buf, ct.byref(bih), 0):
            return False
        with open(path, "wb") as fh:
            fh.write(b"BM" + struct.pack("<IHHI", 54 + stride * h, 0, 0, 54))
            fh.write(bytes(bih)[:40])
            fh.write(buf.raw)
        return True
    finally:
        if old:
            g.SelectObject(mdc, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mdc)
        u.ReleaseDC(hwnd, hdc)
