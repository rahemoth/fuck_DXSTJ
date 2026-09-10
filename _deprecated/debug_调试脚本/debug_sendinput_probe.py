# -*- coding: utf-8 -*-
"""SendInput KEYEVENTF_UNICODE 自包含验证探针:
弹出 tkinter 输入框并强制聚焦,向其键入 Unicode 文本,读回内容比对。
用于隔离"键入事件本身是否可用"(与学习通无关)。

用法:
    .venv\\Scripts\\python.exe -u debug_sendinput_probe.py
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.controller.input import _send_unicode_key

TEXT = "√6你好"

root_ok = None
result = {"text": None}


def worker():
    global root_ok
    time.sleep(0.8)                       # 等主循环起来
    root_ok.focus_force()
    root_ok.attributes("-topmost", True)
    entry.focus_set()
    entry.icursor("end")
    time.sleep(0.5)
    for ch in TEXT:
        _send_unicode_key(ch)
        time.sleep(0.05)
    time.sleep(0.5)
    result["text"] = entry.get()
    root_ok.after(0, root_ok.destroy)


def main():
    global root_ok, entry
    import tkinter as tk

    root = tk.Tk()
    root_ok = root
    root.title("SendInput 探针")
    root.geometry("360x80")
    entry = tk.Entry(root, font=("Arial", 20))
    entry.pack(padx=10, pady=20, fill="x")
    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()

    got = result["text"] or ""
    print(f"期望: {TEXT!r}")
    print(f"实得: {got!r}")
    print("结论:", "SendInput UNICODE 键入 OK" if got == TEXT else "!! 键入事件未生效")


if __name__ == "__main__":
    main()
