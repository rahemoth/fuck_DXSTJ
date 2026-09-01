# -*- coding: utf-8 -*-
"""
网页版执行器:通过 CDP 直连浏览器(Playwright attach),DOM 读题 → LLM 作答 → JS 点击。

与桌面版 executor 同构(run/stop/emit),GUI Worker 可直接承载。

使用方式:
- 程序拉起默认浏览器的专用实例(独立 user-data-dir + 调试端口)
  注:Chromium 136+ 在默认配置目录下会静默忽略 --remote-debugging-port,
  必须使用独立数据目录;专用实例与用户日常浏览器互不干扰,可同时运行
- 首次使用需在专用实例中登录学习通一次,之后登录态保留
- 用户在专用实例中打开作业/考试页面,程序自动发现并注入

优势(相对客户端 OCR 方案):
- DOM 读题零 OCR 错误;题目全部在文档流中,无需滚动导航
- JS 派发点击,不需要窗口前台/焦点
"""
import os
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


# ---------- 注入页面的 JS:滚动找新题(长页懒加载/需滚动) ----------

SCROLL_JS = r"""() => {
    let moved = false;
    const before = window.scrollY || document.documentElement.scrollTop || 0;
    window.scrollBy(0, Math.floor(window.innerHeight * 0.8));
    const after = window.scrollY || document.documentElement.scrollTop || 0;
    if (after !== before) moved = true;
    // 内层滚动容器(学习通部分版式题目区是 div 滚动而非窗口滚动)
    for (const el of document.querySelectorAll('div')) {
        if (el.scrollHeight > el.clientHeight + 100 && el.clientHeight > 200) {
            const b = el.scrollTop;
            el.scrollTop = el.scrollHeight;
            if (el.scrollTop !== b) moved = true;
        }
    }
    return moved;
}"""


class StopRequested(Exception):
    pass


# ---------- 浏览器 CDP 连接管理(模块级,供执行器与测试连接共用) ----------

def _port_open(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_browser_exe(browser: str = "") -> str | None:
    """探测浏览器可执行文件(注册表 App Paths + 常见安装路径)。

    :param browser: "" = 任意(Chrome 优先); "edge" / "chrome" = 指定浏览器
    """
    import os
    import winreg

    if browser in ("edge", "chrome"):
        names = ({"edge": "msedge.exe", "chrome": "chrome.exe"}[browser],)
    else:
        names = ("chrome.exe", "msedge.exe")

    # 浏览器安装子路径:exe 名 → {环境变量: 相对路径}
    subs = {
        "chrome.exe": {
            "LOCALAPPDATA": r"Google\Chrome\Application\chrome.exe",
            "PROGRAMFILES": r"Google\Chrome\Application\chrome.exe",
            "PROGRAMFILES(X86)": r"Google\Chrome\Application\chrome.exe",
        },
        "msedge.exe": {
            "PROGRAMFILES": r"Microsoft\Edge\Application\msedge.exe",
            "PROGRAMFILES(X86)": r"Microsoft\Edge\Application\msedge.exe",
        },
    }

    candidates = []
    for name in names:
        # 注册表 App Paths
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}") as k:
                candidates.append(winreg.QueryValueEx(k, "")[0])
        except OSError:
            pass
        # 常见安装路径
        for env, sub in subs[name].items():
            base = os.environ.get(env)
            if base:
                candidates.append(os.path.join(base, sub))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _managed_profile_dir() -> str:
    """程序专用浏览器配置目录(独立于用户日常浏览器,登录态/标签页互不影响)"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "fuck_DXSTJ", "browser-profile")
    os.makedirs(d, exist_ok=True)
    return d


def _launch_managed_browser(port: int, web_cfg: dict, stop_check=None,
                            extra_args: list | None = None) -> None:
    """拉起程序专用浏览器实例(独立 user-data-dir + 调试端口)。

    端口已开则直接返回(复用现有专用实例)。
    :param extra_args: 附加启动参数(如插件模式的 --load-extension)
    """
    if _port_open(port):
        return

    browser = web_cfg.get("default_browser", "")
    if not browser:
        raise RuntimeError(
            "未设置默认浏览器。请先在【设置 → 网页版】中选择默认浏览器(Edge / Chrome)。")

    browser_name = {"edge": "Edge", "chrome": "Chrome"}[browser]
    logger.info(f"端口 {port} 无浏览器,自动拉起 {browser_name}(程序专用配置,不影响日常浏览器)...")
    exe = _find_browser_exe(browser)
    if not exe:
        raise RuntimeError(
            f"未找到 {browser_name}。请确认已安装,或在设置中改选另一浏览器。")
    profile = _managed_profile_dir()
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--restore-last-session",
    ] + list(extra_args or [])
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(1)
        if stop_check:
            stop_check()
        if _port_open(port):
            return
    raise RuntimeError(
        "专用浏览器启动后调试端口仍不可达。\n"
        "常见原因:浏览器启动失败或被安全软件拦截,\n"
        "请手动运行浏览器确认可用后重试。")


def _ensure_cdp_browser(pw, port: int, web_cfg: dict, stop_check=None):
    """确保调试端口可用并返回 CDP 浏览器对象。

    方案:程序专用浏览器实例(独立 user-data-dir + 调试端口)。
    Chromium 136+ 出于安全考虑,默认配置目录下 --remote-debugging-port 会被
    静默忽略,因此必须使用独立数据目录。该实例与用户日常浏览器互不干扰
    (数据目录不同 = 独立进程树,可同时运行,无需杀/重启用户浏览器)。

    首次使用需在弹出的专用浏览器窗口中登录一次学习通,之后登录态保留。
    """
    url = f"http://127.0.0.1:{port}"
    _launch_managed_browser(port, web_cfg, stop_check=stop_check)
    return pw.chromium.connect_over_cdp(url)


def test_connection(web_cfg: dict) -> tuple[bool, str]:
    """GUI 测试连接用:检测调试端口与学习通页面。只读不改动(不杀/不拉浏览器)。
    返回 (是否成功, 说明文本)。"""
    from playwright.sync_api import sync_playwright

    port = web_cfg.get("cdp_port", 9222)
    keywords = web_cfg.get("url_keywords", ["chaoxing", "mooc"])

    if not _port_open(port):
        return False, (
            f"调试端口 {port} 无浏览器(未就绪属正常)。\n"
            f"点「开始」将自动拉起程序专用浏览器窗口(独立配置,不影响日常浏览器),\n"
            f"首次使用请在弹出的窗口中登录学习通一次。")

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

    def _extract_all(self, page) -> list[tuple]:
        """遍历页面所有 frame 抽取题目(学习通做题页题目在 iframe 里,
        page.evaluate 只作用于主框架)。返回 [(frame, item), ...]。"""
        items = []
        for frame in page.frames:
            try:
                res = frame.evaluate(EXTRACT_JS)
            except Exception:
                continue   # 跨域 frame 等无法注入的情况
            if res:
                items.extend((frame, it) for it in res)
        return items

    def _loop(self, page):
        q_delay = self.web_cfg.get("q_delay", [3, 8])
        opt_delay = self.web_cfg.get("opt_delay", [0.5, 1.5])
        dry_run = self.cfg["action"].get("dry_run", False)
        idle_rounds = 0

        while not self._stop.is_set():
            self._check_stop()
            items = self._extract_all(page)
            if not items:
                logger.warning("页面未识别到题目容器(选择器未命中),请确认当前是做题页")
                return

            pending = 0
            for frame, it in items:
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
                        ret = frame.evaluate(CLICK_JS, [it["idx"], lb])
                        if ret != "ok":
                            logger.warning(f"[网页] 选项 {lb} 点击失败: {ret}")
                        time.sleep(self._rand(opt_delay))
                else:
                    logger.info("[网页] dry-run:跳过点击")
                self.processed.add(q.key)
                self.done_count += 1
                time.sleep(self._rand(q_delay))

            if pending:
                idle_rounds = 0
                time.sleep(1.5)
                continue

            # 无待处理题:滚动找新题(长作业页可能懒加载)
            scrolled = False
            for frame in {id(f): f for f, _ in items}.values():
                try:
                    if frame.evaluate(SCROLL_JS):
                        scrolled = True
                except Exception:
                    pass
            if scrolled:
                idle_rounds = 0
                time.sleep(1.5)
                continue
            idle_rounds += 1
            if idle_rounds >= 2:
                logger.info("网页版所有题目处理完毕。如需提交,请在页面手动提交")
                return
            time.sleep(1.5)

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
