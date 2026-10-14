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


# ------------------------------------------------------------------ liveness
def is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if _WIN:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
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
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            return int(code.value)
        return None
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------------------ windows of a pid
def top_windows(pid: int) -> list[int]:
    """Visible-or-not top-level HWNDs owned by pid (Windows only)."""
    if not _WIN:
        return []
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        p = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            found.append(int(hwnd))
        return True

    u32.EnumWindows(proc(cb), 0)
    return found


def window_class(hwnd: int) -> str:
    if not _WIN:
        return ""
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 255)
    return buf.value


def window_title(hwnd: int) -> str:
    if not _WIN:
        return ""
    import ctypes
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 511)
    return buf.value


def modal_dialogs(pid: int, main_hwnd: int | None = None) -> list[tuple[int, str]]:
    """Visible dialogs of pid as (hwnd, title): '#32770' windows, or any visible owned window while
    its owner (the main window) is disabled - which is how Qt/WPF/WinForms modal boxes look."""
    if not _WIN:
        return []
    import ctypes
    u32 = ctypes.windll.user32
    u32.GetWindow.restype = ctypes.c_void_p
    out = []
    for h in top_windows(pid):
        if h == main_hwnd or not u32.IsWindowVisible(h):
            continue
        owner = u32.GetWindow(h, 4)                      # GW_OWNER
        if window_class(h) == "#32770" or (owner and not u32.IsWindowEnabled(owner)):
            out.append((h, window_title(h)))
    return out


def foreground_pid() -> int | None:
    if not _WIN:
        return None
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    h = u32.GetForegroundWindow()
    if not h:
        return None
    p = wintypes.DWORD()
    u32.GetWindowThreadProcessId(h, ctypes.byref(p))
    return int(p.value)


# ------------------------------------------------------------------ hang detection
def is_hung(target: int, pid: bool = False, timeout_ms: int = 1000) -> bool:
    """True if the window (hwnd) - or, with pid=True, any top window of that pid - is not
    pumping messages (IsHungAppWindow or SendMessageTimeoutW(WM_NULL, SMTO_ABORTIFHUNG))."""
    if not _WIN:
        return False
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    hwnds = top_windows(target) if pid else [target]
    for hwnd in hwnds:
        if not u32.IsWindowVisible(hwnd) and pid:
            continue
        if u32.IsHungAppWindow(hwnd):
            return True
        res = ctypes.c_size_t()
        ok = u32.SendMessageTimeoutW(hwnd, 0, 0, 0, 0x0002, int(timeout_ms), ctypes.byref(res))
        if not ok:
            return True
    return False


# ------------------------------------------------------------------ elevation / integrity
def is_elevated(pid: int | None = None) -> bool | None:
    """Is the process (default: this one) elevated? None if unknown / not Windows."""
    if not _WIN:
        return None
    import ctypes
    from ctypes import wintypes
    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    h = k32.GetCurrentProcess() if pid is None else k32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return None            # access denied typically means the target is more privileged
    tok = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):   # TOKEN_QUERY
            return None
        elev = wintypes.DWORD()
        size = wintypes.DWORD()
        if not adv.GetTokenInformation(tok, 20, ctypes.byref(elev), ctypes.sizeof(elev),
                                       ctypes.byref(size)):         # TokenElevation
            return None
        return bool(elev.value)
    finally:
        if tok:
            k32.CloseHandle(tok)
        if pid is not None:
            k32.CloseHandle(h)


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


def kill_tree(pid: int | None) -> bool:
    """Kill pid and all descendants. Returns True if the root is gone afterwards."""
    if not pid or pid <= 0 or pid == os.getpid():
        return False
    if _WIN:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))], capture_output=True,
                           timeout=10, creationflags=0x08000000)
        except (OSError, subprocess.SubprocessError):
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
    """PrintWindow into a 24-bit BMP file via ctypes (Windows only)."""
    if not _WIN:
        return False
    import ctypes
    from ctypes import wintypes
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0 or w * h > 64_000_000:
        return False
    hdc = u32.GetWindowDC(hwnd)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    old = g32.SelectObject(mdc, bmp)
    try:
        if not u32.PrintWindow(hwnd, mdc, 2):      # PW_RENDERFULLCONTENT
            return False

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        stride = (w * 3 + 3) & ~3
        bih = BIH(ctypes.sizeof(BIH), w, h, 1, 24, 0, stride * h, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(stride * h)
        if not g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0):
            return False
        with open(path, "wb") as fh:
            fh.write(b"BM" + struct.pack("<IHHI", 54 + stride * h, 0, 0, 54))
            fh.write(bytes(bih)[:40])
            fh.write(buf.raw)
        return True
    finally:
        g32.SelectObject(mdc, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(hwnd, hdc)
