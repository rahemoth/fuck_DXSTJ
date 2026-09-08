# -*- coding: utf-8 -*-
"""临时调整学习通窗口的客户区尺寸(诊断用,按客户区目标尺寸换算外框)。
用法: python debug_resize.py <client_w> <client_h>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ctypes

from core.config import load_config
from core.controller.window import WindowCapture

u = ctypes.windll.user32


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def main():
    cw, ch = int(sys.argv[1]), int(sys.argv[2])
    cfg = load_config()
    win = WindowCapture(cfg["window"]["title_keywords"],
                        cfg["window"]["capture_method"])
    hwnd = win.find()
    assert hwnd, "未找到学习通窗口"
    wr = RECT()
    u.GetWindowRect(hwnd, ctypes.byref(wr))
    cl, ct, cr, cb = win.client_rect_screen()
    border_x = (wr.right - wr.left) - (cr - cl)   # 外框与客户区尺寸差
    border_y = (wr.bottom - wr.top) - (cb - ct)
    ow, oh = cw + border_x, ch + border_y
    ok = u.SetWindowPos(hwnd, 0, wr.left, wr.top, ow, oh, 0x0004)  # SWP_NOZORDER
    print(f"客户区 {cr - cl}x{cb - ct} -> {cw}x{ch} (外框 {ow}x{oh}) ok={ok}")


if __name__ == "__main__":
    main()
