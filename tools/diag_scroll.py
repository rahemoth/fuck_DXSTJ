# -*- coding: utf-8 -*-
"""诊断2:前台窗口检查 + 点击聚焦后滚动 + WM_MOUSEWHEEL 消息滚动"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pyautogui
import win32gui
import win32con
import win32api

from core.config import load_config
from core.controller.window import WindowCapture
from core.vision.ocr import OcrEngine
from core.vision.locator import QuestionLocator

cfg = load_config()
win = WindowCapture(cfg["window"]["title_keywords"], cfg["window"]["capture_method"])
win.find()
win.bring_to_front()
time.sleep(0.5)

fg = win32gui.GetForegroundWindow()
print(f"前台窗口: hwnd={fg} [{win32gui.GetWindowText(fg)}]  目标: hwnd={win.hwnd} [{win32gui.GetWindowText(win.hwnd)}]")

ocr = OcrEngine()
loc = QuestionLocator()
l, t, r, b = win.client_rect_screen()


def dump(tag):
    qs = loc.locate_all(ocr.run(win.screenshot()))
    desc = ", ".join(f"#{q.number}(y={q.option_centers.get('A', ('?','?'))[1] if q.option_centers else '?'})" for q in qs) or "无题目"
    print(f"{tag}: {desc}")


dump("初始")

# 方法1: 点击页面空白处聚焦后滚动
cx, cy = l + 400, (t + b) // 2
pyautogui.click(cx, cy)
time.sleep(0.5)
dump("点击后")

pyautogui.moveTo(cx, cy)
pyautogui.scroll(-6)
time.sleep(1.0)
dump("pyautogui滚动后")

# 方法2: win32 PostMessage WM_MOUSEWHEEL
WHEEL_DELTA = 120
lparam = win32api.MAKELONG(cx, cy)  # 屏幕坐标
for _ in range(3):
    win32gui.PostMessage(win.hwnd, win32con.WM_MOUSEWHEEL,
                         win32api.MAKELONG(0, -WHEEL_DELTA * 3), lparam)
    time.sleep(0.3)
time.sleep(1.0)
dump("PostMessage滚动后")

# 方法3: 发给子窗口(CEF_RenderWidgetHostWindow 等)
def try_children(hwnd, depth=0):
    results = []
    for child in win32gui.GetWindow(hwnd, win32con.GW_CHILD) and _enum_children(hwnd):
        pass
    return results

def _enum_children(hwnd):
    kids = []
    def cb(h, _):
        kids.append(h)
        return True
    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return kids

kids = _enum_children(win.hwnd)
print(f"子窗口数: {len(kids)}")
for child in kids[:10]:
    print(f"  child hwnd={child} class={win32gui.GetClassName(child)}")

for child in kids:
    win32gui.PostMessage(child, win32con.WM_MOUSEWHEEL,
                         win32api.MAKELONG(0, -WHEEL_DELTA * 3), lparam)
time.sleep(1.0)
dump("子窗口PostMessage滚动后")
