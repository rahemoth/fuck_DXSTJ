# -*- coding: utf-8 -*-
"""
调试工具:dump 学习通窗口的 OCR 全量结果,用于标定 roi.json 锚点。

用法:
    python tools/dump_ocr.py             # 打印 OCR 文本块
    python tools/dump_ocr.py --save      # 同时保存截图到 logs/screenshot.png
    python tools/dump_ocr.py --parse     # 尝试解析题目结构
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import load_config
from core.log import setup_logging, get_logger
from core.controller.window import WindowCapture
from core.vision.ocr import OcrEngine
from core.vision.locator import QuestionLocator


def main():
    setup_logging()
    logger = get_logger("dump_ocr")

    save = "--save" in sys.argv
    parse = "--parse" in sys.argv

    cfg = load_config()
    win = WindowCapture(cfg["window"]["title_keywords"], cfg["window"]["capture_method"])
    if not win.find():
        logger.error("未找到学习通窗口,请先打开学习通PC客户端")
        return

    win.bring_to_front()  # 确保窗口未最小化且在前台(BitBlt 截屏区域需要)
    import time
    time.sleep(0.5)
    img = win.screenshot()
    if save:
        out = Path("logs") / "screenshot.png"
        out.parent.mkdir(exist_ok=True)
        img.save(out)
        logger.info(f"截图已保存: {out.resolve()}")

    ocr = OcrEngine(cfg["ocr"]["confidence_threshold"])
    blocks = ocr.run(img)

    print(f"\n{'='*70}\nOCR 结果({len(blocks)} 个文本块),坐标为客户区 (x1,y1,x2,y2):\n{'='*70}")
    for b in blocks:
        print(f"  [{b.box[0]:4d},{b.box[1]:4d} -> {b.box[2]:4d},{b.box[3]:4d}] "
              f"conf={b.confidence:.2f}  {b.text}")

    if parse:
        locator = QuestionLocator()
        questions = locator.locate_all(blocks)
        print(f"\n{'='*70}\n题目解析结果({len(questions)} 题):\n{'='*70}")
        for q in questions:
            print(f"  题号 {q.number} [{q.qtype}] 可答={q.is_answerable}")
            print(f"    题干: {q.stem}")
            for k, v in q.options.items():
                print(f"    选项 {k}: {v}  点击坐标={q.option_centers.get(k)}")
        nb = locator.find_next_button(blocks)
        print(f"  下一题按钮: {nb.text if nb else '未找到'} @ {nb.center if nb else '-'}")


if __name__ == "__main__":
    main()
