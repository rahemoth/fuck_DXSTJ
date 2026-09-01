# -*- coding: utf-8 -*-
"""网页版驱动注入 JS 的冒烟测试:用本机 Edge(headless)验证 DOM 抽取与点击。
无 Edge 环境自动跳过。"""
import pytest

from core.web.driver import EXTRACT_JS, CLICK_JS

HTML = """
<html><body>
<div class="TiMu">
  <div class="colorShallow">(单选题)</div>
  <div class="Cy_txt">使用花括号定义的数据类型是()</div>
  <ul>
    <li><label><input type="radio" name="q1">A. 列表</label></li>
    <li><label><input type="radio" name="q1">B. 元组</label></li>
    <li><label><input type="radio" name="q1">C. 集合</label></li>
    <li><label><input type="radio" name="q1">D. 字典</label></li>
  </ul>
</div>
<div class="TiMu">
  <div class="colorShallow">(多选题)</div>
  <div class="Cy_txt">可变数据类型包括()</div>
  <div><input type="checkbox" name="q2"><span>A. 列表</span></div>
  <div><input type="checkbox" name="q2"><span>B. 字典</span></div>
</div>
<div class="TiMu">
  <div class="colorShallow">(判断题)</div>
  <div class="Cy_txt">元组是不可变数据类型()</div>
  <ul>
    <li><label><input type="radio" name="q3">对</label></li>
    <li><label><input type="radio" name="q3">错</label></li>
  </ul>
</div>
<div class="TiMu">
  <div class="colorShallow">(单选题)</div>
  <div class="Cy_txt">无 input 的退化形态()</div>
  <div>A. 选项一</div>
  <div>B. 选项二</div>
</div>
</body></html>
"""


def _edge_page():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(channel="msedge", headless=True)
    except Exception:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pw.stop()
            pytest.skip("本机无可用的 Edge/Chrome")
    page = browser.new_page()
    page.set_content(HTML)
    return pw, browser, page


def test_extract_and_click():
    pw, browser, page = _edge_page()
    try:
        items = page.evaluate(EXTRACT_JS)
        assert len(items) == 4

        q1, q2, q3, q4 = items
        assert q1["qtype"] == "single" and "花括号" in q1["stem"]
        assert q1["labels"] == ["A", "B", "C", "D"]
        assert "列表" in q1["texts"][0] and not q1["answered"]

        assert q2["qtype"] == "multiple" and q2["labels"] == ["A", "B"]

        assert q3["qtype"] == "judge" and q3["labels"] == ["对", "错"]

        # 退化形态:无 input,靠 A. 前缀行识别
        assert q4["labels"] == ["A", "B"]

        # 点击 Q1 选项 D → radio 选中
        assert page.evaluate(CLICK_JS, [0, "D"]) == "ok"
        items2 = page.evaluate(EXTRACT_JS)
        assert items2[0]["answered"] is True

        # 多选点 A、B
        page.evaluate(CLICK_JS, [1, "A"])
        page.evaluate(CLICK_JS, [1, "B"])
        items3 = page.evaluate(EXTRACT_JS)
        assert items3[1]["answered"] is True

        # 判断题点"对"
        assert page.evaluate(CLICK_JS, [2, "对"]) == "ok"
        assert page.evaluate(EXTRACT_JS)[2]["answered"] is True

        # 越界/不存在的选项返回明确错误
        assert page.evaluate(CLICK_JS, [99, "A"]) == "no-container"
        assert page.evaluate(CLICK_JS, [0, "Z"]) == "no-option"
    finally:
        browser.close()
        pw.stop()
