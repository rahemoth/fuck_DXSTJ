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
        try:
            self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            logger.info(f"已连接调试端口 {port}")
        except Exception:
            logger.info(f"端口 {port} 无浏览器,尝试自动拉起 Chrome/Edge ...")
            exe = self._find_browser_exe()
            if not exe:
                raise RuntimeError(
                    f"未找到可用的 Chrome/Edge。请手动以调试端口启动浏览器:\n"
                    f"  chrome.exe --remote-debugging-port={port}\n"
                    f"(需先完全退出浏览器,以保留登录状态)")
            subprocess.Popen([exe, f"--remote-debugging-port={port}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # 等待调试端口就绪
            for _ in range(20):
                time.sleep(1)
                self._check_stop()
                try:
                    self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:
                    continue
            else:
                raise RuntimeError("浏览器启动后调试端口仍不可达,请检查浏览器是否被已有实例占用")

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
        """探测本机 Chrome/Edge 可执行文件"""
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
