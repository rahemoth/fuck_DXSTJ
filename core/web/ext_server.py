# -*- coding: utf-8 -*-
"""
插件桥:本地 HTTP 服务,接收浏览器插件(content script)的请求。

- GET  /config :插件轮询,返回启停状态与延时参数
- POST /solve  :插件提交题目 → 调用 LLM Solver → 返回答案
- POST /report :插件上报事件(题目/答案/完成),转发到 GUI

主程序(ExtensionExecutor)持有本服务,插件在浏览器侧自动轮询工作。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.agent.llm import LLMClient
from core.agent.solver import Solver
from core.log import get_logger
from core.vision.locator import Question

logger = get_logger("web.ext_bridge")


class ExtBridge:
    """桥状态与业务逻辑(HTTP Handler 只做协议层)"""

    def __init__(self, cfg: dict, emit=None):
        self.cfg = cfg
        self.emit = emit or (lambda kind, data: None)
        self.enabled = threading.Event()
        self._solver_lock = threading.Lock()
        self.solver = Solver(LLMClient(cfg["llm"]),
                             max_retries=cfg["llm"].get("max_retries", 1))
        self.done_count = 0
        self.fail_count = 0
        self._done = threading.Event()

    # ---------- 插件请求的业务处理 ----------

    def config(self) -> dict:
        action = self.cfg["action"]
        web = self.cfg.get("web") or {}
        return {
            "enabled": self.enabled.is_set(),
            "dry_run": action.get("dry_run", False),
            "q_delay": web.get("q_delay", [3, 8]),
            "opt_delay": web.get("opt_delay", [0.5, 1.5]),
        }

    def solve(self, payload: dict) -> dict:
        options = payload.get("options") or {}
        q = Question(number=payload.get("number") or 0,
                     qtype=payload.get("qtype") or "single",
                     stem=payload.get("stem") or "",
                     options=options)
        if not q.is_answerable:
            return {"ok": False, "error": "选项不足或题干为空"}
        try:
            # LLM 调用串行化:防多 frame 并发把限流打爆
            with self._solver_lock:
                answer = self.solver.solve(q)
            return {"ok": True, "answer": answer}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def report(self, payload: dict) -> dict:
        event = payload.get("event")
        data = payload.get("data") or {}
        if event == "question":
            self.emit("question", data)
        elif event == "answer":
            self.emit("answer", data)
            self.done_count += 1
            logger.info(f"[插件] 题目{data.get('number', '?')} 答案: {data.get('answer')}")
        elif event == "fail":
            self.fail_count += 1
            logger.warning(f"[插件] {data.get('msg', '求解失败')}")
        elif event == "log":
            logger.info(f"[插件] {data.get('msg', '')}")
        elif event == "hello":
            logger.info(f"[插件] 已注入页面: {(data.get('url') or '')[:80]}")
        elif event == "frame-done":
            logger.info(f"[插件] {data.get('msg', '题目已全部完成')}")
            self._done.set()
        # frame-empty:外层框架无题目,忽略
        return {"ok": True}

    # ---------- 执行器查询 ----------

    def is_done(self) -> bool:
        return self._done.is_set()


def make_handler(bridge: ExtBridge):
    class Handler(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self.path == "/config":
                self._json(bridge.config())
            else:
                self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            if self.path == "/solve":
                self._json(bridge.solve(payload))
            elif self.path == "/report":
                self._json(bridge.report(payload))
            else:
                self._json({"ok": False, "error": "not found"}, 404)

        def log_message(self, *args):
            pass   # 静默访问日志(轮询频繁)

    return Handler


def start_bridge_server(bridge: ExtBridge, port: int) -> ThreadingHTTPServer:
    """启动桥 HTTP 服务(守护线程),返回 server 对象(用于结束时 shutdown)"""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(bridge))
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
