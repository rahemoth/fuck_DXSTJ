# -*- coding: utf-8 -*-
"""
插件模式执行器:浏览器扩展注入方案的入口。

流程:
1. 启动本地桥 HTTP 服务(插件轮询 /config、请求 /solve、上报 /report)
2. 以 --load-extension 拉起程序专用浏览器(插件随浏览器自动装载,
   all_frames 注入天然覆盖做题页 iframe)
3. 等待停止或插件报告全部完成;GUI 事件(题目/答案/统计)与桌面版同构

与 CDP 方案(driver.WebExecutor)的区别:
- 读题/点击都在插件 content script 里做,主程序只提供答案服务
- 无需 Playwright 逐 frame 注入,跨域 iframe 也不受影响
"""
import threading
import time

from core.config import ROOT
from core.log import get_logger
from core.web.ext_server import ExtBridge, start_bridge_server

logger = get_logger("web.ext_executor")

# 插件目录(随仓库分发的未打包 MV3 扩展)
EXTENSION_DIR = ROOT / "webextension"


class ExtensionExecutor:
    """插件注入做题主循环(与桌面版 Executor 同构,GUI Worker 可直接承载)"""

    def __init__(self, cfg: dict, emit=None):
        self.cfg = cfg
        self.web_cfg = cfg.get("web") or {}
        self.emit = emit or (lambda e: None)
        self._stop = threading.Event()
        self.bridge = ExtBridge(cfg, emit=self._emit)

    # ---------- 生命周期 ----------

    def stop(self):
        self._stop.set()
        self.bridge.enabled.clear()   # 插件下次轮询即停止工作
        logger.info("已请求停止(插件模式)")

    def _check_stop(self):
        if self._stop.is_set():
            raise StopRequested()

    # ---------- 主入口 ----------

    def run(self):
        logger.info("===== 插件模式开始执行 =====")
        server = None
        try:
            self._check_stop()
            port = self.web_cfg.get("ext_bridge_port", 9876)
            server = start_bridge_server(self.bridge, port)
            logger.info(f"插件桥服务已启动: http://127.0.0.1:{port}")

            self._launch_browser()
            self.bridge.enabled.set()
            logger.info("插件已激活:请在浏览器中打开学习通做题页(已打开则自动开始)")

            # 等待:用户停止 / 插件报告全部完成
            while not self._stop.is_set() and not self.bridge.is_done():
                time.sleep(0.5)
        except StopRequested:
            logger.info("已停止")
        except Exception as e:
            logger.exception(f"插件模式执行异常终止: {e}")
            self._emit("error", {"message": str(e)})
        finally:
            self.bridge.enabled.clear()
            if server:
                server.shutdown()
            summary = (f"插件模式共完成 {self.bridge.done_count} 题,"
                       f"失败 {self.bridge.fail_count} 题")
            logger.info(f"===== 结束:{summary} =====")
            self._emit("done", {"summary": summary})

    # ---------- 浏览器 ----------

    def _launch_browser(self):
        """拉起带插件的专用浏览器(端口已开则复用现有实例)"""
        from core.web.driver import _launch_managed_browser

        cdp_port = self.web_cfg.get("cdp_port", 9222)
        _launch_managed_browser(
            cdp_port, self.web_cfg, stop_check=self._check_stop,
            extra_args=[f"--load-extension={EXTENSION_DIR}"])
        self._verify_extension(cdp_port)

    def _verify_extension(self, cdp_port: int):
        """检测插件是否装载成功(依据我们独有的 xxt-bridge service worker;
        Chrome 137+ 会忽略 --load-extension,Edge 正常支持)"""
        import json as _json
        import urllib.request

        for _ in range(5):   # 插件装载略滞后于浏览器启动
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{cdp_port}/json", timeout=3) as r:
                    targets = _json.loads(r.read())
                if any("xxt-bridge" in (t.get("url") or "") for t in targets):
                    logger.info("浏览器插件已装载并运行")
                    return
            except Exception:
                pass
            time.sleep(1)
        logger.warning(
            "未检测到插件后台服务。若默认浏览器是 Chrome:Chrome 137+ 忽略 "
            "--load-extension 参数,请在【设置 → 网页版】改选 Edge;\n"
            "若为 Edge,打开学习通页面后观察日志是否出现「[插件] 已注入页面」。")

    # ---------- 工具 ----------

    def _emit(self, kind: str, data: dict):
        from core.pipeline.executor import ExecutorEvent
        self.emit(ExecutorEvent(kind, data))


class StopRequested(Exception):
    pass
