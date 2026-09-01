# -*- coding: utf-8 -*-
"""
网页版执行器:通过 CDP 直连用户浏览器(Playwright attach),DOM 读题 → LLM 作答 → JS 点击。

与桌面版 executor 同构(run/stop/emit),GUI Worker 可直接承载。

使用前提:
- 浏览器需以调试端口启动(Chrome/Edge: --remote-debugging-port=9222)
- 未启动时代码会自动拉起本机 Chrome/Edge(使用默认用户配置,需浏览器已完全退出)
- 用户在该浏览器中登录学习通并打开作业/考试页面

优势(相对客户端 OCR 方案):
- DOM 读题零 OCR 错误;题目全部在文档流中,无需滚动导航
- JS 派发点击,不需要窗口前台/焦点
"""
import subprocess
import threading
import time

from core.agent.llm import LLMClient
from core.agent.solver import Solver
from core.log import get_logger
from core.vision.locator import Question

logger = get_logger("web.driver")

# ---------- 注入页面的 JS:抽取题目 ----------

EXTRACT_JS = r"""() => {
    const SEL = '.TiMu, .question, [class*="TiMu"], [class*="timu"]';
    const containers = [...document.querySelectorAll(SEL)]
        .filter(el => (el.innerText || '').trim().length > 10);
    return containers.map((c, idx) => {
        const full = c.innerText || '';

        // 题型
        const typeEl = c.querySelector('.colorShallow, [class*="type"], [class*="Type"]');
        let qtype = 'single';
        const t = ((typeEl ? typeEl.innerText : '') + full.slice(0, 60));
        if (t.includes('判断')) qtype = 'judge';
        else if (t.includes('多选')) qtype = 'multiple';

        // 选项行:优先带 radio/checkbox 的行,退化正则扫 A. 前缀行
        const texts = [], els = [];
        const inputs = c.querySelectorAll('input[type=radio], input[type=checkbox]');
        if (inputs.length) {
            for (const inp of inputs) {
                const row = inp.closest('label, li, div, p') || inp.parentElement;
                if (!row) continue;
                els.push(row); texts.push((row.innerText || '').trim());
            }
        } else {
            const rows = [...c.querySelectorAll('li, label, div, p')]
                .filter(el => el.children.length <= 2 &&
                    /^[A-H][.、．,，:：\s]/.test((el.innerText || '').trim()));
            for (const row of rows) { els.push(row); texts.push((row.innerText || '').trim()); }
        }

        // 选项标签归一化
        const labels = [];
        for (let i = 0; i < texts.length; i++) {
            const m = texts[i].match(/^([A-H])[.、．,，:：\s]/);
            let lb;
            if (m) lb = m[1];
            else if (qtype === 'judge') {
                if (/[对正确√]/.test(texts[i])) lb = '对';
                else if (/[错误×]/.test(texts[i])) lb = '错';
                else lb = null;
            } else lb = String.fromCharCode(65 + i);
            labels.push(lb);
        }

        // 已答状态
        const answered = !!c.querySelector('input[type=radio]:checked, input[type=checkbox]:checked');

        // 题干
        const stemEl = c.querySelector('.Cy_txt, .qtstem, [class*="stem"], [class*="Stem"]');
        let stem = stemEl ? stemEl.innerText.trim() : '';
        if (!stem) {
            stem = full.split('\n').filter(line => {
                const s = line.trim();
                if (!s) return false;
                return !texts.some(t => t.startsWith(s.slice(0, 8))) &&
                       !/^[A-H][.、．,，:：\s]/.test(s);
            }).join(' ').slice(0, 300);
        }
        return {
            idx, qtype,
            stem: stem.replace(/\s+/g, ' ').trim(),
            labels, texts, answered,
        };
    });
}"""

# ---------- 注入页面的 JS:点击指定题目的指定选项 ----------

CLICK_JS = r"""([qi, label]) => {
    const SEL = '.TiMu, .question, [class*="TiMu"], [class*="timu"]';
    const containers = [...document.querySelectorAll(SEL)]
        .filter(el => (el.innerText || '').trim().length > 10);
    const c = containers[qi];
    if (!c) return 'no-container';

    // 与抽取逻辑一致地重建选项行
    const texts = [], els = [];
    const inputs = c.querySelectorAll('input[type=radio], input[type=checkbox]');
    if (inputs.length) {
        for (const inp of inputs) {
            const row = inp.closest('label, li, div, p') || inp.parentElement;
            if (!row) continue;
            els.push(row); texts.push((row.innerText || '').trim());
        }
    } else {
        const rows = [...c.querySelectorAll('li, label, div, p')]
            .filter(el => el.children.length <= 2 &&
                /^[A-H][.、．,，:：\s]/.test((el.innerText || '').trim()));
        for (const row of rows) { els.push(row); texts.push((row.innerText || '').trim()); }
    }

    let target = null;
    for (let i = 0; i < texts.length; i++) {
        const m = texts[i].match(/^([A-H])[.、．,，:：\s]/);
        const lb = m ? m[1] : null;
        if (lb === label) { target = els[i]; break; }
    }
    // 判断题兜底:按文本找
    if (!target) {
        for (let i = 0; i < texts.length; i++) {
            if (label === '对' && /[对正确√]/.test(texts[i])) { target = els[i]; break; }
            if (label === '错' && /[错误×]/.test(texts[i])) { target = els[i]; break; }
        }
    }
    if (!target) return 'no-option';

    target.scrollIntoView({ block: 'center' });
    const r = target.getBoundingClientRect();
    const x = r.left + r.width * (0.3 + Math.random() * 0.4);
    const y = r.top + r.height * (0.3 + Math.random() * 0.4);
    for (const type of ['mousedown', 'mouseup', 'click']) {
        target.dispatchEvent(new MouseEvent(type, {
            bubbles: true, cancelable: true, view: window,
            clientX: x, clientY: y,
        }));
    }
    const inp = target.querySelector('input[type=radio], input[type=checkbox]');
    if (inp && !inp.checked) inp.click();
    return 'ok';
}"""


class StopRequested(Exception):
    pass


# ---------- 浏览器 CDP 连接管理(模块级,供执行器与测试连接共用) ----------

def _port_open(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _browser_process_running() -> bool:
    """是否有 Chrome/Edge 进程在运行(含后台常驻实例)"""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return False
    return any(name in out for name in ("msedge.exe", "chrome.exe"))


def _find_browser_exe() -> str | None:
    """探测本机 Chrome/Edge 可执行文件(注册表 App Paths + 常见安装路径)"""
    import os
    import winreg

    candidates = []
    # 注册表 App Paths
    for name in ("chrome.exe", "msedge.exe"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}") as k:
                candidates.append(winreg.QueryValueEx(k, "")[0])
        except OSError:
            pass
    # 常见安装路径
    for env, sub in (
        ("LOCALAPPDATA", rf"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES", rf"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES(X86)", rf"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES(X86)", rf"Microsoft\Edge\Application\msedge.exe"),
        ("PROGRAMFILES", rf"Microsoft\Edge\Application\msedge.exe"),
    ):
        base = os.environ.get(env)
        if base:
            candidates.append(os.path.join(base, sub))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _ensure_cdp_browser(pw, port: int, web_cfg: dict, stop_check=None):
    """确保调试端口可用并返回 CDP 浏览器对象。

    三种情况:
    1. 端口已开 → 直接连
    2. 端口未开但浏览器已在运行 → 正常启动的浏览器不带调试端口,
       且新拉起的进程会因单实例机制转交后退出,端口永远起不来:
       - restart_browser=true: 自动结束浏览器进程并以调试端口重启
         (Edge 重启后默认恢复标签页,登录态保留)
       - 否则报错并给出操作指引
    3. 浏览器未运行 → 自动以调试端口拉起
    """
    url = f"http://127.0.0.1:{port}"
    if _port_open(port):
        return pw.chromium.connect_over_cdp(url)

    if _browser_process_running():
        if not web_cfg.get("restart_browser", False):
            raise RuntimeError(
                "浏览器已在运行,但未开启调试端口,无法连接。\n"
                "两种解决办法(任选其一):\n"
                "  1. 完全退出浏览器(注意托盘区后台实例也要退出),再点开始,\n"
                "     程序会自动以调试端口重新拉起;\n"
                "  2. 在 config.yaml 的 web 段设置 restart_browser: true,\n"
                "     程序将自动关闭并以调试端口重启浏览器(标签页可恢复,登录态保留)。")
        # 自动重启浏览器
        logger.info("restart_browser=true:结束现有浏览器进程 ...")
        for name in ("msedge.exe", "chrome.exe"):
            subprocess.run(["taskkill", "/F", "/IM", name],
                           capture_output=True, timeout=15)
        for _ in range(10):
            if not _browser_process_running():
                break
            time.sleep(1)
        time.sleep(1)

    logger.info(f"端口 {port} 无浏览器,自动以调试端口拉起 Chrome/Edge ...")
    exe = _find_browser_exe()
    if not exe:
        raise RuntimeError(
            f"未找到可用的 Chrome/Edge。请手动以调试端口启动浏览器:\n"
            f"  chrome.exe --remote-debugging-port={port}")
    subprocess.Popen([exe, f"--remote-debugging-port={port}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(1)
        if stop_check:
            stop_check()
        if _port_open(port):
            return pw.chromium.connect_over_cdp(url)
    raise RuntimeError(
        "浏览器启动后调试端口仍不可达。\n"
        "常见原因:浏览器有残留的后台/托盘进程占用了单实例锁,\n"
        "请在任务管理器中结束所有 msedge/chrome 进程后重试。")


def test_connection(web_cfg: dict) -> tuple[bool, str]:
    """GUI 测试连接用:检测调试端口与学习通页面。只读不改动(不杀/不拉浏览器)。
    返回 (是否成功, 说明文本)。"""
    from playwright.sync_api import sync_playwright

    port = web_cfg.get("cdp_port", 9222)
    keywords = web_cfg.get("url_keywords", ["chaoxing", "mooc"])

    if not _port_open(port):
        if _browser_process_running():
            return False, (
                f"浏览器在运行但调试端口 {port} 未开启(正常启动的浏览器不带端口)。\n"
                f"请完全退出浏览器(含托盘后台)后由程序重新拉起,\n"
                f"或设置 web.restart_browser: true 自动重启浏览器。")
        return False, f"调试端口 {port} 无浏览器。点「开始」将自动拉起 Chrome/Edge。"

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        for ctx in browser.contexts:
            for page in ctx.pages:
                if any(k in (page.url or "") for k in keywords):
                    return True, f"已连接调试端口,并找到学习通页面: {page.url[:70]}"
        return True, "已连接调试端口,但未找到学习通页面(请先打开作业/考试页)"
    except Exception as e:
        return False, f"连接调试端口失败: {e}"
    finally:
        pw.stop()


class WebExecutor:
    """网页版做题主循环(与桌面版 Executor 同构,GUI 可互换承载)"""

    def __init__(self, cfg: dict, emit=None):
        self.cfg = cfg
        self.web_cfg = cfg.get("web") or {}
        self.emit = emit or (lambda e: None)
        self._stop = threading.Event()

        self.llm = LLMClient(cfg["llm"])
        self.solver = Solver(self.llm, max_retries=cfg["llm"].get("max_retries", 1))

        self.done_count = 0
        self.fail_count = 0
        self.processed: set[str] = set()   # 已处理题干 key

    # ---------- 生命周期 ----------

    def stop(self):
        self._stop.set()
        logger.info("已请求停止(网页版)")

    def _check_stop(self):
        if self._stop.is_set():
            raise StopRequested()

    # ---------- 主入口 ----------

    def run(self):
        logger.info("===== 网页版开始执行 =====")
        try:
            page = self._connect_page()
            self._loop(page)
        except StopRequested:
            logger.info("已停止")
        except Exception as e:
            logger.exception(f"网页版执行异常终止: {e}")
            self._emit("error", {"message": str(e)})
        finally:
            summary = f"网页版共完成 {self.done_count} 题,失败 {self.fail_count} 题"
            logger.info(f"===== 结束:{summary} =====")
            self._emit("done", {"summary": summary})

    # ---------- 连接 ----------

    def _connect_page(self):
        """连接调试端口并定位学习通页面;浏览器未启动则自动拉起"""
        from playwright.sync_api import sync_playwright

        port = self.web_cfg.get("cdp_port", 9222)
        keywords = self.web_cfg.get("url_keywords", ["chaoxing", "mooc"])

        self._pw = sync_playwright().start()
        self._browser = _ensure_cdp_browser(
            self._pw, port, self.web_cfg, stop_check=self._check_stop)
        logger.info(f"已连接调试端口 {port}")

        # 定位学习通页面:立即找,找不到轮询等待用户打开
        wait_sec = self.web_cfg.get("wait_page_timeout", 180)
        t0 = time.time()
        while True:
            self._check_stop()
            page = self._find_xxt_page(keywords)
            if page:
                logger.info(f"已定位学习通页面: {page.url[:80]}")
                return page
            if time.time() - t0 > wait_sec:
                raise RuntimeError(
                    f"{wait_sec} 秒内未找到学习通页面(URL 含 {'/'.join(keywords)})。\n"
                    f"请在调试浏览器中打开学习通作业/考试页面后重试")
            logger.info("等待学习通页面打开 ...")
            time.sleep(2)

    def _find_xxt_page(self, keywords):
        for ctx in self._browser.contexts:
            for page in ctx.pages:
                url = page.url or ""
                if any(k in url for k in keywords):
                    return page
        return None

    @staticmethod
    def _find_browser_exe():
        """探测本机 Chrome/Edge 可执行文件(转发模块级实现)"""
        return _find_browser_exe()

    # ---------- 主循环 ----------

    def _loop(self, page):
        q_delay = self.web_cfg.get("q_delay", [3, 8])
        opt_delay = self.web_cfg.get("opt_delay", [0.5, 1.5])
        dry_run = self.cfg["action"].get("dry_run", False)

        while not self._stop.is_set():
            self._check_stop()
            items = page.evaluate(EXTRACT_JS)
            if not items:
                logger.warning("页面未识别到题目容器(选择器未命中),请确认当前是做题页")
                return

            did = 0
            pending = 0
            for it in items:
                self._check_stop()
                q = self._to_question(it)
                if it["answered"] or q.key in self.processed:
                    continue
                pending += 1
                if not q.is_answerable:
                    logger.warning(f"[网页] 题目{it['idx'] + 1} 不可答(选项不足或题干为空),跳过")
                    self.processed.add(q.key)
                    continue

                self._emit("question", {"qtype": q.qtype, "stem": q.stem,
                                        "options": q.options})
                logger.info(f"[网页] 题目{it['idx'] + 1}[{q.qtype}]: {q.stem[:40]}...")
                try:
                    answer = self.solver.solve(q)
                except Exception as e:
                    logger.warning(f"[网页] 题目{it['idx'] + 1} 作答失败: {e}")
                    self.fail_count += 1
                    self.processed.add(q.key)
                    continue
                self._emit("answer", {"answer": answer})
                logger.info(f"[网页] 题目{it['idx'] + 1} 答案: {answer}")

                if not dry_run:
                    for lb in answer:
                        ret = page.evaluate(CLICK_JS, [it["idx"], lb])
                        if ret != "ok":
                            logger.warning(f"[网页] 选项 {lb} 点击失败: {ret}")
                        time.sleep(self._rand(opt_delay))
                else:
                    logger.info("[网页] dry-run:跳过点击")
                self.processed.add(q.key)
                self.done_count += 1
                did += 1
                time.sleep(self._rand(q_delay))

            if pending == 0:
                logger.info("网页版所有可见题目处理完毕。如需提交,请在页面手动提交")
                return
            # 有未处理的(如刚渲染出来)再扫一轮
            time.sleep(2)

    # ---------- 工具 ----------

    @staticmethod
    def _to_question(it: dict) -> Question:
        options = {}
        for lb, text in zip(it["labels"], it["texts"]):
            if lb and lb not in options:
                options[lb] = text
        return Question(number=it["idx"] + 1, qtype=it["qtype"],
                        stem=it["stem"], options=options)

    @staticmethod
    def _rand(rng):
        import random
        return random.uniform(rng[0], rng[1])

    def _emit(self, kind: str, data: dict):
        from core.pipeline.executor import ExecutorEvent
        self.emit(ExecutorEvent(kind, data))
