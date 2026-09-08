# -*- coding: utf-8 -*-
"""填空题输入链路鉴别测试:
定位当前视口内第一个填空题的第1空,依次尝试三种输入方式并各自 OCR 验证:
  A. pyautogui.typewrite("21")      —— ASCII,标准 VK 键码路径
  B. SendInput KEYEVENTF_UNICODE √6 —— VK_PACKET 路径(中文/符号)
每步前 Ctrl+A 覆盖,OCR 核对实际内容,用于定位"键入不生效"的环节。

用法:
    .venv\\Scripts\\python.exe -u debug_fill_test.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image

import pyautogui

from core.config import load_config
from core.controller.input import InputController
from core.controller.window import WindowCapture
from core.vision.ocr import OcrEngine
from core.vision.locator import QuestionLocator


def region_ocr(ocr, win, region, scale=2):
    img = win.screenshot()
    x1, y1, x2, y2 = region
    crop = img.crop((x1, y1, x2, y2))
    crop = crop.resize((crop.size[0] * scale, crop.size[1] * scale),
                       Image.LANCZOS)
    blocks = ocr.run(crop, threshold=0.3)
    return " | ".join(b.text.strip() for b in blocks) or "(无文本)"


def step(ocr, win, inp, blank, q, label, fn):
    print(f"\n--- 步骤 {label} ---")
    inp.click_client(*blank["center"], label=f"题目{q.number} 第{blank['index']}空")
    time.sleep(0.6)
    inp.select_all()
    time.sleep(0.2)
    fn()
    time.sleep(0.8)
    got = region_ocr(ocr, win, blank["region"])
    print(f"区域OCR: {got}")
    return got


def main():
    cfg = load_config()
    win = WindowCapture(cfg["window"]["title_keywords"],
                        cfg["window"]["capture_method"])
    if win.find() is None:
        print("!! 未找到学习通窗口")
        return
    win.bring_to_front()
    time.sleep(0.5)

    ocr = OcrEngine(cfg["ocr"]["confidence_threshold"])
    locator = QuestionLocator()
    inp = InputController(win, cfg["action"])

    img = win.screenshot()
    blocks = ocr.run(img)
    qs = locator.locate_all(blocks, img.size[1], img.size[0])
    fills = [q for q in qs if q.qtype == "fill" and q.blanks]
    if not fills:
        print("当前视口无填空题(先滚动到填空题可见)")
        return
    q = fills[0]
    b = q.blanks[0]
    print(f"目标: 题目{q.number} 第{b['index']}空 点击{b['center']} 检测区{b['region']}")
    print(f"初始OCR: {region_ocr(ocr, win, b['region'])}")

    # A. ASCII 标准键码
    step(ocr, win, inp, b, q, "A: pyautogui.typewrite('21')",
         lambda: pyautogui.typewrite("21"))
    # B. SendInput UNICODE
    step(ocr, win, inp, b, q, "B: SendInput UNICODE '√6'",
         lambda: inp.type_text("√6"))


if __name__ == "__main__":
    main()
