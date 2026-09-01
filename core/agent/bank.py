# -*- coding: utf-8 -*-
"""
题库接口(预留):后续可接入第三方题库 API。

接口约定:
    search(question: Question) -> list[str] | None
    返回答案列表(如 ["A"] / ["A","C"] / ["对"]),无结果返回 None。
LLM 永远作为兜底。
"""
from core.log import get_logger
from core.vision.locator import Question

logger = get_logger("agent.bank")


class QuestionBank:
    """空实现:直接返回 None,流程会走 LLM"""

    def search(self, question: Question) -> list[str] | None:
        return None


# 全局默认题库实例
default_bank = QuestionBank()
