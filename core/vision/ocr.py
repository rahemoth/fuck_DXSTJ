# -*- coding: utf-8 -*-
"""
OCR 识别模块:RapidOCR(MAA 同源 PaddleOCR 离线模型,onnxruntime 推理)。

输出统一的 OcrBlock 列表:文本 + 客户区坐标 + 置信度,
供 locator 做题目定位、executor 做坐标回算。
"""
from dataclasses import dataclass

from PIL import Image

from core.log import get_logger

logger = get_logger("vision.ocr")


@dataclass
class OcrBlock:
    text: str
    box: tuple[int, int, int, int]   # (x1, y1, x2, y2) 客户区坐标
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.box
        return (x1 + x2) // 2, (y1 + y2) // 2

    def __repr__(self):
        return f"OcrBlock({self.text!r} @{self.box} conf={self.confidence:.2f})"


class OcrEngine:
    """RapidOCR 封装(懒加载,首次调用时初始化模型)"""

    _engine = None

    def __init__(self, confidence_threshold: float = 0.55):
        self.threshold = confidence_threshold

    @classmethod
    def _get_engine(cls):
        if cls._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            cls._engine = RapidOCR()
            logger.info("RapidOCR 引擎已初始化")
        return cls._engine

    def run(self, image: Image.Image, threshold: float | None = None) -> list[OcrBlock]:
        """识别 PIL 图像,返回按 y 坐标排序的文本块列表。
        :param threshold: 置信度阈值覆盖(单字符选项等小目标置信度低,
                          executor 会对识别不完整的页面用低阈值重试)"""
        import numpy as np

        th = self.threshold if threshold is None else threshold
        engine = self._get_engine()
        arr = np.asarray(image.convert("RGB"))
        result, _ = engine(arr)

        blocks = []
        if result:
            for box, text, score in result:
                conf = float(score)
                if conf < th:
                    continue
                # box: 4个顶点 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                x1, y1, x2, y2 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
                blocks.append(OcrBlock(text=str(text), box=(x1, y1, x2, y2), confidence=conf))

        blocks.sort(key=lambda b: (b.box[1], b.box[0]))
        return blocks
