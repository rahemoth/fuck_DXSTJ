# -*- coding: utf-8 -*-
"""网课(超星学习通)自动播放执行器:CDP 直连浏览器,在页面各层 frame 注入 JS。

原理(参考 references/ocs.txt 油猴脚本与 学生学习页面.html):
- 学习页(mycourse/stu)是外层框架,#iframe 加载 knowledge/cards 卡片页,
  卡片页内每个任务点是 iframe.ans-insertvideo-online,内部为 video.js 播放器;
- 油猴脚本的做法:找到 video 元素 → 静音/设倍速 → play() → 监听 pause 自动续播
  → ended 视为完成 → 通过顶层 PCount.next(...) 跳转下一任务点/下一章;
- 本执行器用 Playwright frame.evaluate 在各 frame 中执行等价 JS:
  视频层安装保活脚本(静音、倍速、防暂停、随机答视频弹题),
  卡片层统计任务点完成状态,完成则在外层触发跳章。

与 WebExecutor 同构(run/stop/emit),GUI Worker 可直接承载。
"""
import threading
import time

from core.log import get_logger
from core.web.driver import _ensure_cdp_browser, StopRequested

logger = get_logger("web.course")

# ---------- 视频层 JS:安装保活控制并返回播放状态 ----------

CONTROL_JS = r"""(cfg) => {
    const v = document.querySelector('video, #video_html5_api');
    if (!v) return { has: false };
    window.__cxCfg = cfg;
    if (!window.__cxInstalled) {
        window.__cxInstalled = true;
        v.volume = cfg.volume;
        v.muted = cfg.volume === 0;
        // 油猴脚本同款:暂停事件自动续播
        v.addEventListener('pause', () => {
            if (!v.ended) setTimeout(() => v.play().catch(() => {}), 800);
        });
        v.addEventListener('ended', () => { window.__cxDone = true; });
        // 周期兜底:自动点击视频弹题(随机选项 + 提交,油猴 videoQuizStrategy=random 行为)
        setInterval(() => {
            const sub = document.getElementById('videoquiz-submit');
            if (sub) {
                const opts = [...document.querySelectorAll('.ans-videoquiz-opt label')];
                if (opts.length) {
                    opts[Math.floor(Math.random() * opts.length)].click();
                    sub.click();
                }
            }
            try {
                v.playbackRate = window.__cxCfg.rate;
                if (!v.ended && v.paused) v.play().catch(() => {});
            } catch (e) {}
        }, 2000);
    }
    // 主动驱动:未结束则播放并保持倍速/静音
    if (!v.ended) {
        v.playbackRate = cfg.rate;
        v.volume = cfg.volume;
        v.muted = cfg.volume === 0;
        if (v.paused) v.play().catch(() => {});
    }
    // 恢复被播放器隐藏的控制条(OCS fixedVideoProgress)
    const bar = document.querySelector('.vjs-control-bar');
    if (bar) bar.style.opacity = '1';
    return {
        has: true,
        playing: !v.paused && !v.ended,
        ended: v.ended,
        done: !!window.__cxDone,
        time: Math.round(v.currentTime || 0),
        duration: Math.round(v.duration || 0),
    };
}"""

# ---------- 卡片层 JS:统计任务点完成状态 ----------

JOBS_JS = r"""() => {
    const icons = [...document.querySelectorAll('.ans-job-icon')];
    const unfinished = icons.filter(i =>
        !(i.parentElement && i.parentElement.classList.contains('ans-job-finished')));
    return { total: icons.length, unfinished: unfinished.length };
}"""

# ---------- 外层 JS:触发跳转下一任务点/下一章 ----------

JUMP_NEXT_JS = r"""() => {
    // OCS mode=next:利用外层页自带的全局 PCount.next 跳到下一任务点
    const c = document.getElementById('curChapterId');
    const co = document.getElementById('curCourseId');
    const cl = document.getElementById('curClazzId');
    if (!c || !window.PCount) return 'no-pcount';
    window._preChapterId = c.value;
    const tabs = document.querySelectorAll('#prev_tab .prev_ul li');
    PCount.next(String(tabs.length), c.value, co.value, cl.value, '');
    return 'ok-next';
}"""

JUMP_CHAPTER_JS = r"""() => {
    // OCS mode=job 近似实现:点击左侧目录中当前章节之后的下一个章节
    const acts = [...document.querySelectorAll('#coursetree .posCatalog_select')];
    const ai = acts.findIndex(e => e.classList.contains('posCatalog_active'));
    const next = acts.slice(ai + 1).find(e => e.querySelector('.posCatalog_name'));
    if (!next) return 'final';
    next.querySelector('.posCatalog_name').click();
    return 'ok-chapter';
}"""


class CourseExecutor:
    """网课自动播放主循环(与桌面版/网页版执行器同构,GUI 可互换承载)"""

    def __init__(self, cfg: dict, emit=None):
        self.cfg = cfg
        self.web_cfg = cfg["web"]
        course = cfg.get("course") or {}
        self.rate = float(course.get("playback_rate", 1.0))
        self.volume = float(course.get("volume", 0))
        self.mode = course.get("mode", "next")          # next / job

        self.emit = emit or (lambda e: None)
        self._stop = threading.Event()

        self.played_count = 0      # 已播放完成的视频数
        self._jumped_marks = 0     # 已跳转次数(页面标记)
        self._last_jump_ts = 0.0   # 跳转冷却,避免页面切换过渡期误判

    # ---------- 生命周期 ----------

    def stop(self):
        self._stop.set()
        logger.info("已请求停止(网课自动播放)")

    def _check_stop(self):
        if self._stop.is_set():
            raise StopRequested()

    # ---------- 主入口 ----------

    def run(self):
        logger.info("===== 网课自动播放开始 =====")
        try:
            pages = self._connect_pages()
            self._loop(pages)
        except StopRequested:
            logger.info("已停止")
        except Exception as e:
            logger.exception(f"网课自动播放异常终止: {e}")
            self._emit("error", {"message": str(e)})
        finally:
            summary = f"网课自动播放完成 {self.played_count} 个视频"
            logger.info(f"===== 结束:{summary} =====")
            self._emit("done", {"summary": summary})

    # ---------- 连接 ----------

    def _collect_pages(self):
        """按关键字收集学习通页面(每次调用重新枚举,页面列表不会过期)"""
        keywords = self.web_cfg["url_keywords"]
        return [p for ctx in self._browser.contexts for p in ctx.pages
                if any(k in (p.url or "") for k in keywords)]

    def _connect_pages(self):
        """连接调试端口并定位学习通学习页(未启动则自动拉起浏览器)。

        已知坑:端口被残留的程序专用实例占用(只有扩展后台页、无可见窗口)
        时会"连接成功但永远找不到页面" —— 此时应自动清掉残留实例后重新拉起。
        """
        from playwright.sync_api import sync_playwright

        port = self.web_cfg["cdp_port"]

        self._pw = sync_playwright().start()
        self._browser = _ensure_cdp_browser(
            self._pw, port, self.web_cfg, stop_check=self._check_stop)
        logger.info(f"已连接调试端口 {port}")

        wait_sec = self.web_cfg["wait_page_timeout"]
        t0 = time.time()
        cleaned_stale = False
        while True:
            self._check_stop()
            pages = self._collect_pages()
            if pages:
                logger.info(f"已定位学习通页面: {pages[0].url[:80]}")
                return pages

            # 端口上连"一个 http 页面"都没有 → 大概率是残留的专用实例占用端口
            if not self._has_http_page() and not cleaned_stale:
                if self._restart_stale_if_ours(port):
                    cleaned_stale = True
                    continue
                # 端口被用户自己的浏览器占用且无页面,不再反复重试 kill
                cleaned_stale = True   # 降级为纯等待,由用户自行处理

            if time.time() - t0 > wait_sec:
                urls = [p.url[:70] for c in self._browser.contexts
                        for p in c.pages][:10]
                raise RuntimeError(
                    f"{wait_sec} 秒内未找到学习通页面(URL 需含"
                    f" {'/'.join(self.web_cfg['url_keywords'])})。\n"
                    f"请在程序专用浏览器中打开课程学习页后重试。"
                    + (f"\n当前可见页面: {urls}" if urls else "\n当前无可见页面"))

            logger.info("等待学习通页面打开 ...(请在程序专用浏览器中打开课程学习页;"
                        "日常浏览器直接打开是看不到的——程序只连接它拉起的专用实例)")
            time.sleep(2)

    # ---------- 残留实例清理 ----------

    def _has_http_page(self) -> bool:
        return any((p.url or "").startswith("http")
                   for c in self._browser.contexts for p in c.pages)

    def _port_pid(self, port: int) -> int | None:
        """查占用调试端口处于 LISTENING 状态的进程 PID"""
        import re
        import subprocess
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True,
                text=True, timeout=10).stdout
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = re.split(r"\s+", line.strip())
                    return int(parts[-1])
        except Exception as e:
            logger.warning(f"netstat 查询失败: {e}")
        return None

    def _pid_cmdline(self, pid: int) -> str:
        import subprocess
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            out = ""
        return out or ""

    def _restart_stale_if_ours(self, port: int) -> bool:
        """端口被无页面的残留实例占用时处理:
        - 是程序自己的专用实例(命令行含专用 profile 路径) → 结束进程并重新拉起
        - 是用户自己的浏览器 → 给出明确报错提示
        返回是否执行了重启(需要外层继续轮询)"""
        pid = self._port_pid(port)
        cmdline = self._pid_cmdline(pid) if pid else ""
        logger.warning(
            f"调试端口 {port} 已被 PID={pid} 占用且无可见页面,命令行: "
            f"{(cmdline or '<空>')[:160]}")

        if "fuck_DXSTJ" in cmdline and "browser-profile" in cmdline:
            logger.warning("判定为程序残留的专用实例,将结束该进程并重新拉起...")
            import subprocess
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
            time.sleep(2)
            self._browser = _ensure_cdp_browser(
                self._pw, port, self.web_cfg, stop_check=self._check_stop)
            logger.info("残留实例已清理,已重新拉起专用浏览器")
            return True

        if not cmdline:
            raise RuntimeError(
                f"调试端口 {port} 被未知进程占用且无可见页面。\n"
                f"请在任务管理器结束占用者,或在设置中更换 cdp_port 后重试。")

        # 用户自己的浏览器(带调试端口启动)占用了端口
        raise RuntimeError(
            f"调试端口 {port} 被您已存在的浏览器(PID {pid})占用。\n"
            f"请在设置中将 cdp_port 改为其他端口,或关闭该浏览器后重试。")

    # ---------- 主循环 ----------

    def _loop(self, pages=None):
        """主循环。pages 参数仅为兼容签名;每轮重新收集页面列表(防止过期)"""
        cooldown = 20.0   # 跳转后等待页面加载的冷却秒数
        while not self._stop.is_set():
            self._check_stop()
            cfg = {"rate": self.rate, "volume": self.volume}

            videos = []       # [(frame, state)]
            cards_state = None
            stu_frame = None  # 外层学习页(mycourse/stu,PCount 所在)

            for page in self._collect_pages():
                try:
                    page_frames = page.frames
                except Exception:
                    continue   # 页面已导航/关闭,下一轮再取
                for frame in page_frames:
                    url = frame.url or ""
                    try:
                        res = frame.evaluate(CONTROL_JS, cfg)
                    except Exception:
                        continue   # 跨域/不可注入 frame
                    if res and res.get("has"):
                        videos.append((frame, res))

                    if "knowledge/cards" in url and cards_state is None:
                        try:
                            cards_state = frame.evaluate(JOBS_JS)
                        except Exception:
                            pass
                    if "mycourse/stu" in url:
                        stu_frame = frame

            # 当前章节任务点已全部完成 → 在外层学习页触发跳转
            if (cards_state and cards_state["total"] > 0 and
                    cards_state["unfinished"] == 0 and stu_frame and
                    time.time() - self._last_jump_ts > cooldown):
                if self._trigger_jump(stu_frame):
                    self._last_jump_ts = time.time()
                    self._emit("log", {"message": "当前章节任务点已完成,已触发跳转"})
                    time.sleep(8)
                    continue

            if videos:
                self._log_progress(videos)
            time.sleep(3)

    def _trigger_jump(self, frame) -> bool:
        try:
            ret = frame.evaluate(
                JUMP_NEXT_JS if self.mode == "next" else JUMP_CHAPTER_JS)
        except Exception:
            return False
        if ret in ("ok-next", "ok-chapter"):
            logger.info(f"任务点全部完成,已触发跳转({self.mode} 模式): {ret}")
            self.played_count += 1
            return True
        if ret == "final":
            logger.info("已达最后一个章节,自动播放结束")
            self._stop.set()
        return False

    def _log_progress(self, videos):
        st = videos[0][1]
        msg = (f"播放中: {st.get('time', 0)}/{st.get('duration', 0)}s "
               f"done={sum(1 for _, s in videos if s.get('done'))}/{len(videos)}")
        logger.info(msg)

    # ---------- 工具 ----------

    def _emit(self, kind, data):
        from core.pipeline.executor import ExecutorEvent
        self.emit(ExecutorEvent(kind, data))
