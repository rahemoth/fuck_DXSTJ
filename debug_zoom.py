# -*- coding: utf-8 -*-
"""模拟 executor._ocr_zoom_band:裁剪指定区域放大3倍重OCR,
验证缩小窗口下放大重识别兜底是否有效。
用法: python debug_zoom.py [y1 y2]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image

from core.config import load_config
from core.controller.window import WindowCapture
from core.vision.ocr import OcrEngine


def main():
    cfg = load_config()
    win = WindowCapture(cfg["window"]["title_keywords"],
                        cfg["window"]["capture_method"])
    if win.find() is None:
        print("!! 未找到学习通窗口")
        return
    img = win.screenshot()
    print(f"截图尺寸 {img.size}")

    # 默认:Q1 选项区(顶部视图,题干@363,下一题锚点@648)
    y1 = int(sys.argv[1]) if len(sys.argv) > 1 else 360
    y2 = int(sys.argv[2]) if len(sys.argv) > 2 else 660
    x1, x2 = 100, min(1000, img.size[0])

    band = img.crop((x1, y1, x2, y2))
    band = band.resize((band.size[0] * 3, band.size[1] * 3), Image.LANCZOS)
    band.save("debug_band.png")

    ocr = OcrEngine(0.3)
    blocks = ocr.run(band)
    print(f"\n== zoom band y{y1}~{y2} 放大3倍 OCR(0.3) 共{len(blocks)} 块 ==")
    for b in blocks:
        # 映射回原坐标
        bx1, by1, bx2, by2 = b.box
        ox1, oy1 = x1 + bx1 // 3, y1 + by1 // 3
        print(f"  {b.text!r:20s} 原坐标@({ox1},{oy1}) conf={b.confidence:.2f}")

    # 对照:不放大直接 OCR 同一区域
    raw = ocr.run(img.crop((x1, y1, x2, y2)))
    print(f"\n== 对照:同区域不放大直接 OCR(0.3) 共{len(raw)} 块 ==")
    for b in raw:
        bx1, by1, _, _ = b.box
        print(f"  {b.text!r:20s} @({x1 + bx1},{y1 + by1}) conf={b.confidence:.2f}")


if __name__ == "__main__":
    main()
