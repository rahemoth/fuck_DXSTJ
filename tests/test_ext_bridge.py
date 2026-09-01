# -*- coding: utf-8 -*-
"""插件桥(ExtBridge)单元测试:配置下发 / 求解 / 事件统计,不发真实网络请求"""
import json
import threading
import urllib.request

import pytest

from core.web.ext_server import ExtBridge, start_bridge_server

CFG = {
    "llm": {"base_url": "http://x", "api_key": "k", "model": "m",
            "timeout": 5, "max_retries": 0},
    "action": {"dry_run": True},
    "web": {"q_delay": [3, 8], "opt_delay": [0.5, 1.5]},
}


@pytest.fixture()
def bridge():
    return ExtBridge(CFG)


def test_config_reports_state(bridge):
    cfg = bridge.config()
    assert cfg["enabled"] is False
    assert cfg["dry_run"] is True
    assert cfg["q_delay"] == [3, 8]
    bridge.enabled.set()
    assert bridge.config()["enabled"] is True


def test_solve_rejects_unanswerable(bridge):
    r = bridge.solve({"stem": "", "qtype": "single", "options": {}})
    assert r["ok"] is False


def test_solve_calls_solver(bridge, monkeypatch):
    monkeypatch.setattr(bridge.solver, "solve", lambda q: ["A"])
    r = bridge.solve({"number": 1, "qtype": "single", "stem": "题干",
                      "options": {"A": "甲", "B": "乙"}})
    assert r == {"ok": True, "answer": ["A"]}


def test_solve_swallows_solver_error(bridge, monkeypatch):
    def boom(q):
        raise RuntimeError("llm down")
    monkeypatch.setattr(bridge.solver, "solve", boom)
    r = bridge.solve({"number": 1, "qtype": "single", "stem": "题干",
                      "options": {"A": "甲", "B": "乙"}})
    assert r["ok"] is False and "llm down" in r["error"]


def test_report_counts_and_done(bridge):
    events = []
    bridge.emit = lambda kind, data: events.append((kind, data))
    bridge.report({"event": "answer", "data": {"number": 1, "answer": ["A"]}})
    bridge.report({"event": "fail", "data": {"msg": "x"}})
    bridge.report({"event": "frame-done", "data": {"msg": "done"}})
    assert bridge.done_count == 1
    assert bridge.fail_count == 1
    assert bridge.is_done() is True
    assert events[0][0] == "answer"


def test_http_endpoints_cors():
    """真实起一个桥服务验证 HTTP 协议层(含 CORS 预检)"""
    bridge = ExtBridge(CFG)
    port = 19876   # 测试专用端口,避免与运行中的实例冲突
    server = start_bridge_server(bridge, port)
    try:
        # 预检
        req = urllib.request.Request(f"http://127.0.0.1:{port}/solve", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=3) as r:
            assert r.status == 204
            assert r.headers["Access-Control-Allow-Origin"] == "*"
        # config
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=3) as r:
            cfg = json.loads(r.read())
            assert cfg["enabled"] is False
        # report
        body = json.dumps({"event": "answer",
                           "data": {"number": 2, "answer": ["B"]}}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/report",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            assert json.loads(r.read())["ok"] is True
        assert bridge.done_count == 1
    finally:
        server.shutdown()
