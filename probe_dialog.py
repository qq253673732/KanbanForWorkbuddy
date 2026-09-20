"""诊断：serve.py 那条弹窗调用路径到底成不成立。

不改动 serve.py，用**完全相同的调用方式**（同样的命令行参数、同样重定向 stdout/stderr）
起一个子进程，然后客观枚举这个子进程有没有真的创建出窗口：

  - 有可见窗口        → 弹窗能力正常，问题在调用它的那个进程/会话上下文
  - 建了窗口但不可见  → 窗口被创建却不在可见桌面上（z 序 / 桌面隔离）
  - 根本没有窗口      → 子进程建不出窗口（会话、作业对象或窗口站被限制）

用法（在任意终端里跑，脚本自己会收尾，不会卡住）：

    python probe_dialog.py
"""

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

DIALOG_TITLE = "诊断探针：随便选一个目录或点取消"


def session_id() -> str:
    """进程所在的 Windows 会话号。0 = 服务会话（没有用户桌面）。"""
    try:
        sid = wintypes.DWORD(0)
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return str(sid.value)
    except Exception as exc:                      # noqa: BLE001
        return f"<查询失败 {exc}>"
    return "<未知>"


def context_info() -> dict:
    """窗口站 / 桌面 / 可见性——判断当前进程能不能把窗口画到用户屏幕上。"""
    info: dict = {}
    u32 = ctypes.WinDLL("user32", use_last_error=True)

    class USEROBJECTFLAGS(ctypes.Structure):
        _fields_ = [("fInherit", wintypes.BOOL),
                    ("fReserved", wintypes.BOOL),
                    ("dwFlags", wintypes.DWORD)]

    def name_of(handle: int, idx: int = 2) -> str:
        buf = ctypes.create_unicode_buffer(256)
        need = wintypes.DWORD(0)
        if handle and u32.GetUserObjectInformationW(handle, idx, buf,
                                                    ctypes.sizeof(buf), ctypes.byref(need)):
            return buf.value
        return f"<查询失败 err={ctypes.get_last_error()}>"

    station = u32.GetProcessWindowStation()
    info["窗口站"] = name_of(station)
    flags = USEROBJECTFLAGS()
    need = wintypes.DWORD(0)
    if u32.GetUserObjectInformationW(station, 1, ctypes.byref(flags),
                                     ctypes.sizeof(flags), ctypes.byref(need)):
        visible = bool(flags.dwFlags & 0x1)
        info["窗口站可见"] = "是" if visible else "否（WSF_VISIBLE=0，窗口没地方显示）"
    info["本进程桌面"] = name_of(u32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId()))

    ctypes.set_last_error(0)
    desk = u32.OpenInputDesktop(0, False, 0x0001)
    if desk:
        info["输入桌面"] = f"{name_of(desk)}（可访问）"
        u32.CloseDesktop(desk)
    else:
        info["输入桌面"] = f"打不开（err={ctypes.get_last_error()}）"
    return info


def windows_of(pid: int) -> list[tuple[int, str, str, bool]]:
    """枚举某个进程的所有顶层窗口：(句柄, 类名, 标题, 是否可见)。"""
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[tuple[int, str, str, bool]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        owner = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            title = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(hwnd, title, 512)
            found.append((hwnd, cls.value, title.value, bool(u32.IsWindowVisible(hwnd))))
        return True

    u32.EnumWindows(callback, 0)
    return found


def main() -> int:
    print("== 上下文 ==")
    print(f"python      : {sys.executable}")
    print(f"会话号      : {session_id()}   (0 = 服务会话，没有用户桌面)")
    print(f"SESSIONNAME : {os.environ.get('SESSIONNAME', '<无此环境变量>')}")
    for key, value in context_info().items():
        print(f"{key:<12}: {value}")

    print("\n== 用 serve.py 完全相同的方式起子进程 ==")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$d.Description = '{DIALOG_TITLE}';"
        "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)"
        "{'PICKED:' + $d.SelectedPath} else {'CANCEL'}"
    )
    started = time.perf_counter()
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
    )
    print(f"子进程 PID {proc.pid} 已启动，观察它是否建出窗口（最多 15 秒）…")

    visible: list[tuple[int, str, str, bool]] = []
    all_windows: list[tuple[int, str, str, bool]] = []
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        all_windows = windows_of(proc.pid)
        visible = [w for w in all_windows if w[3]]
        if visible:
            break
        time.sleep(0.25)

    cost = time.perf_counter() - started
    print(f"观测结束，用时 {cost:.2f} 秒")
    if all_windows:
        print(f"子进程的顶层窗口 {len(all_windows)} 个：")
        for hwnd, cls, title, vis in all_windows:
            print(f"    hwnd={hwnd} 可见={'是' if vis else '否'} 类={cls!r} 标题={title!r}")
    else:
        print("子进程一个顶层窗口都没有。")

    if proc.poll() is None:
        proc.kill()
        print("（探针已关掉这个子进程）")
    try:
        out, err = proc.communicate(timeout=5)
    except Exception:                             # noqa: BLE001
        out = err = ""
    print(f"子进程退出码 {proc.returncode} | stdout={((out or '').strip())!r} "
          f"stderr={((err or '').strip()[:200])!r}")

    print("\n== 结论 ==")
    if visible:
        print("子进程建出了可见窗口 → 这条调用路径本身没问题，")
        print("问题出在真正跑 serve.py 的那个进程上下文（启动方式/会话）。")
    elif all_windows:
        print("窗口建出来了但不可见 → 窗口没落到可见桌面上（桌面隔离或 z 序问题）。")
    else:
        print("子进程建不出窗口 → 当前上下文不允许创建 UI（会话/作业对象/窗口站被限制）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
