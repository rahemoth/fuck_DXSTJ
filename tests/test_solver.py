# -*- coding: utf-8 -*-
"""测试:Solver 答案解析(mock LLM,不发真实请求)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core.agent.solver import Solver, AnswerParseError
from core.vision.locator import Question
from core.vision.ocr import OcrBlock


class MockLLM:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)

    def chat(self, system, user):
        return self.replies.pop(0)


def make_single_question():
    q = Question(qtype="single", stem="1+1=?")
    for label, text, y in (("A", "1", 100), ("B", "2", 120), ("C", "3", 140)):
        q.options[label] = text
        q.option_centers[label] = (50, y + 10)
    return q


def test_parse_plain_json():
    s = Solver(MockLLM(['{"answer": "B"}']))
    assert s.solve(make_single_question()) == ["B"]


def test_parse_markdown_wrapped():
    s = Solver(MockLLM(['```json\n{"answer": "A"}\n```']))
    assert s.solve(make_single_question()) == ["A"]


def test_parse_with_extra_text():
    s = Solver(MockLLM(['好的,答案是 {"answer": "C"} ,请参考']))
    assert s.solve(make_single_question()) == ["C"]


def test_parse_compact_multiple():
    q = Question(qtype="multiple", stem="多选", options={"A": "x", "B": "y", "C": "z"})
    s = Solver(MockLLM(['{"answer": "AC"}']))
    assert s.solve(q) == ["A", "C"]


def test_parse_multiple_list():
    q = Question(qtype="multiple", stem="多选", options={"A": "x", "B": "y", "C": "z"})
    s = Solver(MockLLM(['{"answer": ["A", "C"]}']))
    assert s.solve(q) == ["A", "C"]


def test_parse_judge():
    q = Question(qtype="judge", stem="地球是圆的", options={"对": "对", "错": "错"})
    s = Solver(MockLLM(['{"answer": "对"}']))
    assert s.solve(q) == ["对"]


def test_judge_synonym_mapping():
    """模型返回'正确'也能映射到'对'"""
    q = Question(qtype="judge", stem="地球是圆的", options={"对": "对", "错": "错"})
    s = Solver(MockLLM(['{"answer": "正确"}']))
    assert s.solve(q) == ["对"]


def test_retry_on_bad_json():
    s = Solver(MockLLM(["这不是JSON", '{"answer": "B"}']), max_retries=1)
    assert s.solve(make_single_question()) == ["B"]


def test_invalid_option_rejected():
    """答案选项不在题目选项中,应解析失败"""
    s = Solver(MockLLM(['{"answer": "E"}']), max_retries=0)
    with pytest.raises(AnswerParseError):
        s.solve(make_single_question())


def test_lowercase_answer():
    s = Solver(MockLLM(['{"answer": "b"}']))
    assert s.solve(make_single_question()) == ["B"]
