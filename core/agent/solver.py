# -*- coding: utf-8 -*-
"""
答题决策模块:
- 题库优先(预留),LLM 兜底
- 组装 prompt,要求模型返回 JSON
- 解析答案并映射为可点击的选项标签列表
"""
import json
import re

from core.agent.llm import LLMClient
from core.agent.bank import default_bank
from core.log import get_logger
from core.vision.locator import Question

logger = get_logger("agent.solver")

SYSTEM_PROMPT = """你是一个答题助手。根据题目和选项选出正确答案。

必须严格遵守:
1. 只输出一个 JSON 对象,不要输出任何其他内容(不要markdown代码块标记)。
2. 单选题格式: {"answer": "A"}
3. 多选题格式: {"answer": ["A", "B"]}(选项按字母顺序)
4. 判断题格式: {"answer": "对"} 或 {"answer": "错"}
5. answer 中的选项字母必须是大写,且必须是题目中实际存在的选项。
6. 如果完全无法确定,选择你认为最可能的一个,不要留空。"""


class AnswerParseError(Exception):
    pass


class Solver:
    def __init__(self, llm_client: LLMClient, bank=None, max_retries: int = 1):
        self.llm = llm_client
        self.bank = bank or default_bank
        self.max_retries = max_retries

    # ---------- 对外接口 ----------

    def solve(self, question: Question) -> list[str]:
        """返回答案标签列表,如 ["A"] / ["A","C"] / ["对"]"""
        # 1. 题库优先
        bank_answer = self._try_bank(question)
        if bank_answer:
            logger.info(f"题库命中: {bank_answer}")
            return bank_answer

        # 2. LLM 兜底(带重试)
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                reply = self.llm.chat(SYSTEM_PROMPT, question.to_prompt_text())
                return self._parse_answer(reply, question)
            except AnswerParseError as e:
                last_err = e
                logger.warning(f"答案解析失败(第{attempt + 1}次): {e}")
        raise AnswerParseError(f"LLM 答案解析最终失败: {last_err}")

    # ---------- 内部 ----------

    def _try_bank(self, question: Question) -> list[str] | None:
        try:
            return self.bank.search(question)
        except Exception as e:
            logger.warning(f"题库查询异常(忽略): {e}")
            return None

    def _parse_answer(self, reply: str, question: Question) -> list[str]:
        """解析 LLM 回复中的 JSON 答案,并校验为题目实际存在的选项"""
        data = self._extract_json(reply)
        raw = data.get("answer")
        if raw is None:
            raise AnswerParseError(f"JSON 无 answer 字段: {data}")

        # 归一化为标签列表
        if isinstance(raw, str):
            raw = raw.strip()
            # "AC" -> ["A","C"]; "A" -> ["A"]; "对"/"错" 保持
            if question.qtype != "judge" and len(raw) > 1 and all(c in "ABCDEF" for c in raw.upper()):
                labels = list(raw.upper())
            else:
                labels = [raw]
        elif isinstance(raw, list):
            labels = [str(x).strip() for x in raw]
        else:
            raise AnswerParseError(f"answer 类型非法: {type(raw)}")

        # 校验
        valid = []
        for lb in labels:
            lb_up = lb.upper()
            if question.qtype == "judge":
                if lb in question.options:
                    valid.append(lb)
                elif lb in ("对", "正确", "√", "T", "true", "True"):
                    valid.append("对")
                elif lb in ("错", "错误", "×", "F", "false", "False"):
                    valid.append("错")
            elif lb_up in question.options:
                valid.append(lb_up)
            else:
                logger.warning(f"答案选项 {lb!r} 不在题目选项中,忽略")

        if not valid:
            raise AnswerParseError(f"答案 {labels} 未命中任何有效选项")
        # 去重保序
        seen, result = set(), []
        for v in valid:
            if v not in seen:
                seen.add(v)
                result.append(v)
        return result

    @staticmethod
    def _extract_json(reply: str) -> dict:
        """从回复中提取 JSON(容忍 markdown 代码块、前后废话)"""
        # 剥离 ```json ... ``` 代码块
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", reply, re.S)
        if m:
            reply = m.group(1)
        # 直接查找第一个 { ... } 块
        m = re.search(r"\{.*\}", reply, re.S)
        if not m:
            raise AnswerParseError(f"回复中无 JSON: {reply[:100]}")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise AnswerParseError(f"JSON 解析失败: {e}; 原文: {m.group(0)[:100]}")
        if not isinstance(data, dict):
            raise AnswerParseError(f"JSON 不是对象: {data}")
        return data
