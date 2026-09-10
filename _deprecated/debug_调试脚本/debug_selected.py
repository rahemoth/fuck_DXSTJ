# -*- coding: utf-8 -*-
"""模拟 executor 完整解析链路 + _is_selected,验证缩小窗口下
选中检测是否被左侧导航栏的蓝色元素污染(x<100 在搜索范围 [cx-170,cx+10] 内)。
只读,不点击。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from PIL import Image

from core.config import load_config
from core.controller.window import WindowCapture
from core.vision.ocr import OcrEngine, OcrBlock
from core.vision.locator import QuestionLocator

cfg = load_config()
win = WindowCapture(cfg["window"]["title_keywords"],
                    cfg["window"]["capture_method"])
assert win.find() is not None, "未找到学习通窗口"
img = win.screenshot()
img.save("debug_window.png")
print(f"截图尺寸 {img.size}")

locator = QuestionLocator()
ocr = OcrEngine(cfg["ocr"]["confidence_threshold"])

# ---- 第一步:整页 OCR 0.55 ----
blocks = ocr.run(img)
questions = locator.locate_all(blocks, img.size[1])
target = partial = None
for q in questions:
    if q.key in ("11",):  # Q1 1+1=
        partial = q
print(f"\n[整页0.55] Q1: options={partial.options} complete={partial.complete}"
      f" answerable={partial.is_answerable} anchor_y2={partial.anchor_y2} region_y2={partial.region_y2}")

# ---- 第二步:retry 0.3(整页) ----
blocks3 = ocr.run(img, threshold=0.3)
q3 = locator.locate_all(blocks3, img.size[1])
for q in q3:
    if q.key == "11":
        print(f"[整页0.30] Q1: options={q.options} complete={q.complete} answerable={q.is_answerable}")

# ---- 第三步:zoom band(executor._ocr_zoom_band 同逻辑) ----
y1, y2 = partial.anchor_y2, partial.region_y2
y2 = min(y2, y1 + 420)
rx1, _, rx2, _ = locator.region
x1, x2 = max(0, rx1), min(img.size[0], rx2)
scale = 3
band = img.crop((x1, y1, x2, y2))
band = band.resize((band.size[0] * scale, band.size[1] * scale), Image.LANCZOS)
band_blocks = ocr.run(band, threshold=0.3)
mapped = []
for b in band_blocks:
    bx1, by1, bx2, by2 = b.box
    mapped.append(OcrBlock(text=b.text,
                            box=(x1 + bx1 // scale, y1 + by1 // scale,
                                 x1 + bx2 // scale, y1 + by2 // scale),
                            confidence=b.confidence))
merged = [b for b in blocks if not (y1 <= (b.box[1] + b.box[3]) / 2 < y2)] + mapped
merged.sort(key=lambda b: (b.box[1], b.box[0]))
questions = locator.locate_all(merged, img.size[1])
for q in questions:
    if q.key == "11":
        target = q
print(f"\n[zoom后] Q1: options={target.options}")
print(f"  centers={target.option_centers}")
print(f"  complete={target.complete} reason={target.incomplete_reason!r} answerable={target.is_answerable}")

# ---- 第四步:_is_selected 逐选项分析(含蓝色像素列来源) ----
arr = np.asarray(img.convert("RGB"), dtype=np.int16)
h, w = arr.shape[:2]


def analyze(label, cx, cy):
    yy1, yy2 = max(0, cy - 20), min(h, cy + 20)
    xx1, xx2 = max(0, cx - 170), min(w, cx + 10)
    region = arr[yy1:yy2, xx1:xx2]
    r, g, b = region[:, :, 0], region[:, :, 1], region[:, :, 2]
    blue = ((b > 120) & (b - r > 50) & (b - g > 25))
    colsum = blue.sum(axis=0)
    col_max = int(colsum.max()) if blue.size else 0
    verdict = "SELECTED" if col_max > 20 else "-"
    print(f"\n选项{label} center=({cx},{cy}) 搜索区 x{xx1}~{xx2} y{yy1}~{yy2}"
          f" 单列最大蓝px={col_max} → {verdict}")
    # 蓝像素列分布(只列 >5 的列)
    cols = [(int(xx1 + i), int(c)) for i, c in enumerate(colsum) if c > 5]
    if cols:
        nav = [c for c in cols if c[0] < 100]
        content = [c for c in cols if c[0] >= 100]
        if nav:
            print(f"  ⚠ 导航栏列(x<100): {nav}")
        if content:
            print(f"  内容区列(x>=100): {content}")
    else:
        print("  (无显著蓝列)")


print("\n===== _is_selected 模拟(点击前,理论上应全部未选中) =====")
for lb, (cx, cy) in target.option_centers.items():
    analyze(lb, cx, cy)
