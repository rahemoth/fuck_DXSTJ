# -*- coding: utf-8 -*-
"""诊断3:验证点击选项是否真实生效(对比点击前后选项区域的像素变化)"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from core.config import load_config
from core.controller.window import WindowCapture
from core.controller.input import InputController
from core.vision.ocr import OcrEngine
from core.vision.locator import QuestionLocator

cfg = load_config()
win = WindowCapture(cfg["window"]["title_keywords"], cfg["window"]["capture_method"])
win.find()
win.bring_to_front()
time.sleep(0.5)

ocr = OcrEngine()
loc = QuestionLocator()
inp = InputController(win, cfg["action"])

qs = loc.locate_all(ocr.run(win.screenshot()))
answerable = [q for q in qs if q.is_answerable]
if not answerable:
    print("当前无完整可见题目,先滚动一屏")
    win.post_scroll(2, 400, 375)
    time.sleep(1.0)
    qs = loc.locate_all(ocr.run(win.screenshot()))
    answerable = [q for q in qs if q.is_answerable]

q = answerable[0]
label = sorted(q.options)[0]
cx, cy = q.option_centers[label]
print(f"测试题目 #{q.number} 点击选项 {label}: {q.options[label]!r} @client({cx},{cy})")

def crop(img):
    box = (max(0, cx - 150), max(0, cy - 25), cx + 150, cy + 25)
    return np.asarray(img.crop(box).convert("RGB"), dtype=np.int16)

before = crop(win.screenshot())
inp.click_client(cx, cy, label=f"测试选项{label}", delay=False)
time.sleep(0.8)
after = crop(win.screenshot())

diff = float(np.abs(before - after).mean())
print(f"点击前后像素平均差: {diff:.2f}")
if diff > 3:
    print("=> 点击生效(选项区域发生变化)")
else:
    print("=> 点击可能无效(区域几乎没变化)")
