# -*- coding: utf-8 -*-
"""只读诊断脚本:对当前学习通窗口做 截图 → OCR → 解析 → 标注,不做任何点击。

用途:
1. 诊断"窗口缩小后选项错选":dump 窗口/客户区尺寸、坐标偏移、
   OCR 块与解析结果的标注图(debug_marked.png),肉眼即可比对
   程序认为的选项坐标与页面实际选项位置。
2. 采集填空题/简答题布局:检测题目区域内的水平边框线
   (输入框/文本域的上下边),为输入框定位提供数据。

用法:
    .venv\\Scripts\\python.exe debug_snapshot.py [threshold] [scroll_steps]
    threshold 默认 0.55,可选 0.3(低阈值重试视角)
    scroll_steps 可选:截图前先按 ↓ N 次(查看视口下方内容,如简答题编辑器)

输出(项目根目录):
    debug_window.png  原始截图
    debug_marked.png  标注图(绿=题干锚点,红十字=选项坐标,蓝=检出选中,
                       黄线=疑似输入框边框线)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from core.config import load_config
from core.controller.window import WindowCapture
from core.vision.ocr import OcrEngine
from core.vision.locator import QuestionLocator


def is_selected(img, cx: int, cy: int) -> bool:
    """executor._is_selected 的复刻:选项前蓝色圆圈检测"""
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    h, w = arr.shape[:2]
    y1, y2 = max(0, cy - 20), min(h, cy + 20)
    x1, x2 = max(0, cx - 170), min(w, cx + 10)
    if y2 <= y1 or x2 <= x1:
        return False
    region = arr[y1:y2, x1:x2]
    r, g, b = region[:, :, 0], region[:, :, 1], region[:, :, 2]
    blue = ((b > 120) & (b - r > 50) & (b - g > 25))
    col_max = int(blue.sum(axis=0).max()) if blue.size else 0
    return col_max > 20


def detect_h_lines(img, x1=100, x2=1100):
    """检测近水平的长边线(输入框/文本域边框)。
    边框为浅灰(#DCDFE6 附近):各通道 195~250 且通道差异小。
    返回 [(y, xs, xe)],按 y 排序。"""
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    h, w = arr.shape[:2]
    x2 = min(x2, w)
    region = arr[:, x1:x2]
    r, g, b = region[:, :, 0], region[:, :, 1], region[:, :, 2]
    gray = ((r >= 195) & (r <= 250) & (g >= 195) & (g <= 250)
            & (b >= 200) & (b <= 255) & (np.abs(r - b) < 25) & (np.abs(r - g) < 25))
    # 每行中灰边像素占比超过 55% 视为边线行
    frac = gray.mean(axis=1)
    lines = []
    i = 0
    while i < h:
        if frac[i] > 0.55:
            j = i
            while j < h and frac[j] > 0.55:
                j += 1
            y = (i + j) // 2
            row = gray[i:j].any(axis=0)
            xs = np.argmax(row) if row.any() else 0
            # 连续段起点/终点(忽略行首行尾少量断裂)
            idx = np.where(row)[0]
            lines.append((y, x1 + int(idx[0]), x1 + int(idx[-1])))
            i = j
        else:
            i += 1
    return lines


def main():
    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else 0.55
    scroll_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    cfg = load_config()
    win = WindowCapture(cfg["window"]["title_keywords"],
                        cfg["window"]["capture_method"])
    if win.find() is None:
        print("!! 未找到学习通窗口")
        return
    if scroll_steps:
        from core.controller.input import InputController
        InputController(win, cfg["action"]).arrow_down(scroll_steps)
        import time
        time.sleep(1.0)
    l, t, r, b = win.client_rect_screen()
    print(f"客户区屏幕矩形: ({l},{t})-({r},{b})  尺寸 {r - l}x{b - t}")
    sx, sy = win.client_to_screen(0, 0)
    print(f"client_to_screen(0,0) = ({sx},{sy})  偏移 ({sx - l},{sy - t})")

    img = win.screenshot()
    img.save("debug_window.png")
    print(f"截图尺寸: {img.size}  (与客户区逻辑尺寸一致={img.size == (r - l, b - t)})")

    print(f"\n===== OCR 块 (阈值 {threshold}) =====")
    ocr = OcrEngine(threshold)
    blocks = ocr.run(img)
    for blk in blocks:
        print(f"  {blk.text!r:30s} @({blk.box[0]:4d},{blk.box[1]:4d})"
              f"-({blk.box[2]:4d},{blk.box[3]:4d}) conf={blk.confidence:.2f}")

    print("\n===== 解析题目 =====")
    locator = QuestionLocator()
    questions = locator.locate_all(blocks, img.size[1], img.size[0])
    for q in questions:
        print(f"\n题目{q.number} [{q.qtype}] anchor_y2={q.anchor_y2} region_y2={q.region_y2}")
        print(f"  stem: {q.stem}")
        print(f"  options: {q.options}")
        print(f"  centers: {q.option_centers}")
        if q.qtype == "fill":
            for b in q.blanks:
                print(f"  第{b['index']}空 点击@{b['center']} 检测区{b['region']}")
        if q.qtype == "short_answer":
            print(f"  编辑器点击@{q.editor_center}")
        print(f"  complete={q.complete} reason={q.incomplete_reason!r}")
        for lb, (cx, cy) in q.option_centers.items():
            print(f"  选项{lb} @{(cx, cy)} 选中={is_selected(img, cx, cy)}")

    print("\n===== 疑似输入框边线(黄线) =====")
    lines = detect_h_lines(img)
    for y, xs, xe in lines:
        print(f"  y={y:5d}  x {xs}~{xe}  宽 {xe - xs}")

    # ---- 标注图 ----
    marked = img.convert("RGB").copy()
    draw = ImageDraw.Draw(marked)
    try:
        font = ImageFont.truetype("msyh.ttc", 15)
    except Exception:
        font = ImageFont.load_default()
    for q in questions:
        # 题干锚点绿框
        for blk in blocks:
            if blk.text.strip().startswith((f"{q.number}.", f"{q.number}、")) or (
                    q.stem and q.stem[:8] in blk.text):
                draw.rectangle(blk.box, outline="green", width=2)
                draw.text((blk.box[0], blk.box[1] - 16),
                          f"Q{q.number}锚点", fill="green", font=font)
                break
        # 选项红十字 + 标签
        for lb, (cx, cy) in q.option_centers.items():
            sel = is_selected(img, cx, cy)
            color = "blue" if sel else "red"
            draw.line((cx - 14, cy, cx + 14, cy), fill=color, width=2)
            draw.line((cx, cy - 14, cx, cy + 14), fill=color, width=2)
            draw.text((cx + 16, cy - 8), f"{lb}{'(选)' if sel else ''}",
                      fill=color, font=font)
    for y, xs, xe in lines:
        draw.line((xs, y, xe, y), fill="orange", width=2)
    marked.save("debug_marked.png")
    print("\n已保存 debug_window.png / debug_marked.png (橙线=疑似输入框边线)")


if __name__ == "__main__":
    main()
