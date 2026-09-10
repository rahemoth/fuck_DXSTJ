# -*- coding: utf-8 -*-
"""整页扫描器:方向键逐屏滚动截图 → 图像对齐测偏移 → 拼接长图 →
逐帧 OCR 合并 → 全题一次解析(替代旧的"逐题滚动识别")。

滚动机制:学习通页面滚动条被网页 CSS 隐藏、鼠标滚轮被禁用,唯一可靠
手段是键盘方向键(每按一次 ↓ 精确滚 40px,线性)。所有滚动都先点击
内容区空白列建立焦点(input._focus_content)。

坐标体系:所有题目/文本块坐标为长图坐标(页面绝对坐标)。
作答时用 scroll_to 把目标题滚进视口,再换算 viewport_y = long_y - offset。

关键机制:
- 偏移测量:相邻两帧之间在内容区做双向条带对齐搜索(prev 高条带在
  cur 中正向搜索 + cur 低条带在 prev 中反向搜索,联合多数表决),
  滚动多少像素不依赖按键数的换算精度;
- OCR 流水线:每帧截图后立即提交后台单线程 OCR,主线程继续滚动
  截取下一帧,扫描结束时 join 等待(OCR 与滚动等待重叠,免串行等待);
- 接缝合并:每帧只保留"上一帧没完整覆盖"的块(帧顶 y >= 重叠区-50),
  既无重复也不会漏(重叠区上沿的块在上一帧离底边 >=50px,检出可靠);
- px_per_press:每按一次方向键页面滚动的像素数(标称 40),扫描中实测
  累计,scroll_to 据此估算按键数,再以对齐测量校正,最多迭代 4 轮;
- 偏移先验:current_offset 记录上次实测偏移与之后的按键位移估计,
  优先在先验 ±_PRIOR_WINDOW 内精搜(作答阶段最大单点热点),
  失败再降采样粗搜全范围兑底,不会因窗口化失去定位恢复手段。
"""
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from core.controller.input import InputController
from core.controller.window import WindowCapture
from core.log import get_logger
from core.vision.locator import QuestionLocator
from core.vision.ocr import OcrBlock, OcrEngine

logger = get_logger("pipeline.scanner")

# 扫描帧数默认上限(可通过配置 action.max_scan_frames 覆盖)。
# 注意:每帧步长受单批按键上限约束(8键×40px=320px),并非 0.7 视口——
# 732px 高视口下 40 帧仅覆盖 ~17.5 屏(12480px),超长作业页会被截断,
# 故默认放宽到 120 帧(≈38400px+视口,足够 50+ 题的页面)
_MAX_FRAMES_DEFAULT = 120
# 单条带"良好对齐"的紧残差阈值:同位置截图 bit-exact(实测 0.000),
# 错位条带即便白对白也 >1.5。吸顶标题/光标污染带残差常>30。
# 用它统计每个偏移下"同时良好对齐的条带数"(内条数),多数表决:
# 正确偏移让尽量多带同时近零残差,单条污染带不影响计数。
_STRIP_MATCH_DIFF = 1.0
# 最终共识残差超过该值视为对齐失败
_ALIGN_DIFF_LIMIT = 12.0
# 页面停止移动的最小位移(视为到底/按键未生效)
_MIN_MOVE = 15
# 方向键标称步长:每按一次 ↓ 滚动的像素数(实测学习通为 40)
_PRESS_PX = 40
# 单批按键防呆上限(实际步长由 scan_step_ratio 与条带可见性公式约束,
# 见 scan();此值仅防止配置了过大的 step_ratio 时一次性滚过多)
_MAX_PRESSES = 16
# 到底疑似时的补按确认批次数(小步):确认页面纹丝不动前不敢轻易判
# 到底,补按批太大会有“中途停顿”误判后一帧跳幅过大、条带全部越界的风险
_CONFIRM_PRESSES = 8
# current_offset 先验窗口:先验 ±该值内步长 1 精搜(按键位移估计误差
# 与点击引起的自动滚动都在该量级内)
_PRIOR_WINDOW = 200
# 固定顶栏高度(tab 条 + "作业/暂时保存/提交" 条, y≈44~90):
# 该区域不随滚动移动,每帧截图都含同一份悬浮 chrome。拼长图时第 2 帧起
# 跳过此带,否则 chrome 黑字会被贴进长图中部,污染 current_offset 对齐
# (真实偏移处的残差=页面内容 vs chrome 黑字,反大于错误偏移处的白对白)
_CHROME_H = 95


@dataclass
class ScanResult:
    """整页扫描结果(所有坐标均为长图坐标)"""
    long_img: Image.Image                       # 拼接长图
    blocks: list[OcrBlock] = field(default_factory=list)   # 合并后的 OCR 块
    questions: list = field(default_factory=list)         # Question(长图坐标)
    max_offset: int = 0                         # 页面最大滚动偏移
    px_per_press: float | None = None           # 实测 每按键滚动像素数
    frames: list = field(default_factory=list)  # [(offset, img)]
    scanner: "PageScanner | None" = None        # 回引(供 scroll_to 委托)

    @property
    def viewport_h(self) -> int:
        return self.long_img.size[1] - self.max_offset

    def scroll_to(self, target: int) -> int:
        """委托回 PageScanner(作答阶段用)"""
        return self.scanner.scroll_to(target) if self.scanner else -1


class PageScanner:
    def __init__(self, window: WindowCapture, ocr: OcrEngine,
                 locator: QuestionLocator, input_ctl: InputController,
                 cfg: dict, check_stop=None):
        self.window = window
        self.ocr = ocr
        self.locator = locator
        self.input = input_ctl
        self.cfg = cfg
        self._check_stop = check_stop or (lambda: None)
        self.result: ScanResult | None = None
        # ---- current_offset 先验状态(作答阶段性能热点) ----
        self._lng_gray: np.ndarray | None = None   # 长图灰度缓存(避免每次 convert)
        self._lng_src: Image.Image | None = None    # 缓存对应的 long_img(身份校验)
        self._offset_prior: int | None = None       # 上次实测偏移(先验)
        self._presses_delta = 0                     # 自上次实测后按键位移估计(px)
        self._last_presses = _CONFIRM_PRESSES       # 最近一批按键数(兑底估算用)
        self._ppp_live: float | None = None         # 扫描中实时 px/press

    # ==================== 扫描 ====================

    def scan(self) -> ScanResult | None:
        """整页扫描。中途异常返回 None(调用方回退旧循环);
        页面无可滚内容时循环 1 轮自然结束(单题翻页页由执行器兑底)。"""
        self._check_stop()
        self.input.press_home()                     # 回顶(内部先点内容区建焦点)
        time.sleep(self.cfg["action"].get("page_wait", 1.0))
        img0 = self.window.screenshot()
        w, h = img0.size          # PIL size = (宽, 高)
        frames: list[tuple[int, Image.Image]] = [(0, img0)]
        offset = 0
        ppp: float | None = None                   # 实测每按键像素
        self._ppp_live = None
        step_ratio = float(self.cfg["action"].get("scan_step_ratio", 0.7))
        settle = float(self.cfg["action"].get("scan_settle", 0.7))
        zero_streak = 0                            # 连续"无位移"批次数
        max_frames = int(self.cfg["action"].get("max_scan_frames",
                                                _MAX_FRAMES_DEFAULT))
        # 目标步长 = step_ratio × 视口高,但受三重硬约束(保证大步长下
        # 帧间对齐仍可靠、拼接无空隙):
        # a) ≤ 0.77h - _CHROME_H:0.77/0.88 高条带滚后仍在 cur 的内容区
        #    (y0-moved ≥ _CHROME_H,顶部固定 chrome 带不随滚动,条带落在
        #    该带时对不上内容);
        # b) ≤ 0.63h:反向对齐的 0.30 低条带(y1b=0.37h)滚后不出 prev 底边;
        # c) ≤ h - _CHROME_H - 20:第 2 帧起只贴视口 [_CHROME_H,h) 行,
        #    步长超过该值相邻帧贴片区在长图上会出现未覆盖白条。
        # 满足 a+b 时正/反向至少各 1~2 条带参与多数表决(紧残差阈值下
        # 错位偏移拿不到内条,少数带也足以定夺),另有单点估算/全帧降采样
        # 两重兑底。h=732 视口下步长≈440px(11 键),h=800 下≈480px(12 键),
        # 相比旧固定 320px 帧数减少约 1/3,重复 OCR 面积大幅降低。
        step_cap = min(int(h * 0.77) - _CHROME_H, int(h * 0.63),
                       h - _CHROME_H - 20)
        step_px = max(160, min(int(h * step_ratio), step_cap))
    
        # OCR 流水线:截图后立即提交后台单线程 OCR,主线程滚动期间
        # 后台推理上一帧(滚动等待 ~1.2s/帧与 OCR ~1.7s/帧重叠)。
        # 单 worker 保证 OCR 调用本身串行(引擎为类级单例,虽 onnxruntime
        # session.run 线程安全,串行化可完全整开 CPU 争用);扫描结束时
        # join 取回全部结果再拼接。中途放弃的帧(到底补按批/对齐失败)
        # 不取 result,其 future 静默完成不阻塞。
        ocr_pool = ThreadPoolExecutor(max_workers=1)
        ocr_futs: list = [ocr_pool.submit(self.ocr.run, img0)]
        try:
            while len(frames) < max_frames:
                self._check_stop()
                prev_img = frames[-1][1]
                # 估算按键数:目标页面步长向下取整到整键,防实际步长
                # 超出上述条带可见性/拼接覆盖上限
                presses = max(2, min(_MAX_PRESSES,
                                     step_px // int(ppp or _PRESS_PX)))
                self._last_presses = presses
                self.input.arrow_down(presses)
                time.sleep(settle)
                img = self.window.screenshot()
                if img.size != (w, h):
                    logger.warning("扫描中窗口尺寸变化,放弃整页扫描")
                    return None
                fut = ocr_pool.submit(self.ocr.run, img)
                moved = self.frame_offset(prev_img, img)
                if moved is None:
                    # 无法条带对齐:区分"页面没动(到底)"与"动了但测不出"
                    # (中途大片空白区)"——后者继续扫有漏采风险,回退逐题模式
                    static = self._page_static(prev_img, img)
                    logger.info(f"[align] frame_offset=None, _page_static={static}")
                    if static:
                        moved = 0
                    else:
                        logger.warning("视口对齐失败(底部无内容且页面有变化),放弃整页扫描")
                        return None
                if moved < _MIN_MOVE:
                    # 单批无位移:可能是到底,也可能是方向键中途停顿(实测滚动
                    # 存在个别批次只滚几十像素后恢复的现象)。补按一批(内含
                    # 重新焦点点击)再确认;连续两批都纹丝不动才判定到底。
                    # 补按批用小步(_CONFIRM_PRESSES):误判后补滚的幅度也
                    # 保持在条带可靠测量范围内。
                    self.input.arrow_down(_CONFIRM_PRESSES)
                    presses += _CONFIRM_PRESSES
                    self._last_presses = presses
                    time.sleep(settle)
                    img2 = self.window.screenshot()
                    if img2.size != (w, h):
                        return None
                    fut2 = ocr_pool.submit(self.ocr.run, img2)
                    moved2 = self.frame_offset(prev_img, img2)
                    if moved2 is None:
                        if self._page_static(prev_img, img2):
                            moved2 = 0
                        else:
                            logger.warning("视口对齐失败(底部无内容且页面有变化),放弃整页扫描")
                            return None
                    if moved2 < _MIN_MOVE:
                        zero_streak += 1
                        logger.info(f"连续{zero_streak}批无位移(疑似到底)")
                        if zero_streak >= 2:
                            break
                        continue
                    moved, img, fut = moved2, img2, fut2
                zero_streak = 0
                if moved > h - 80:
                    # 跳幅超过一屏会有未覆盖区域,放弃
                    logger.warning(f"单次滚动位移过大({moved}px),存在漏采风险,回退逐题模式")
                    return None
                offset += moved
                frames.append((offset, img))
                ocr_futs.append(fut)
                # ppp 更新:仅在实际位移接近按键请求量时更新
                # (页面到底被截短的那一帧 moved 远小于请求量,比例失真)。
                # 容差 5%:键盘滚动线性精确(±2px/键),到底截短帧差值
                # 通常落在 5%~15% 区间,旧 15% 容差会把它们误当作可信样本
                # 拉低 ppp,影响后续步长/回卷按键数估算
                requested = presses * (ppp or _PRESS_PX)
                if ppp is None or abs(moved - requested) <= requested * 0.05:
                    ppp_new = moved / presses
                    ppp = ppp_new if ppp is None else (ppp + ppp_new) / 2
                    self._ppp_live = ppp
                logger.info(f"扫描进度:第{len(frames)}帧,累计偏移 {offset}px "
                            f"(本帧滚动 {moved}px, px/press≈{ppp and round(ppp, 1)})")
    
            max_offset = offset
            if len(frames) >= max_frames:
                # 退出原因=帧数上限而非到底(到底时上面会有"连续N批无位移"):
                # 最后一帧仍在正常滚动,页面尾部内容未进长图,解析出的题目不全
                logger.warning(f"达到扫描帧数上限({max_frames}帧,累计 {offset}px),"
                               "页面可能未扫完,尾部题目将被遗漏;"
                               "可在配置 action.max_scan_frames 调大")
            logger.info(f"扫描结束:共 {len(frames)} 帧,页面已扫高约 {max_offset + h}px")
    
            # ---- 拼接长图 ----
            # 第 2 帧起跳过顶部固定 chrome 带(见 _CHROME_H):该带是悬浮控件,
            # 贴进长图会在内容区留下黑字假内容,破坏作答阶段的对齐测量。
            # 跳过带对应的页面行由上一帧的真实内容覆盖(步进约 0.6 屏,重叠足够)。
            long_img = Image.new("RGB", (w, max_offset + h), "white")
            for fi, (off, im) in enumerate(frames):
                if fi == 0:
                    long_img.paste(im, (0, off))
                else:
                    long_img.paste(im.crop((0, _CHROME_H, w, h)),
                                   (0, off + _CHROME_H))
    
            # ---- 等待后台 OCR 完成 + 接缝合并 ----
            # 最后一帧的 OCR 与上面的长图拼接重叠执行;帧按 offset 顺序入列,
            # future 完成顺序无关,按序取回结果即可
            frame_blocks = [f.result() for f in ocr_futs]
            blocks = self._merge_blocks(frames, frame_blocks)
            # 底部裁剪检查放宽:长图含整页,不存在"贴近视口底部"的截断
            questions = self.locator.locate_all(blocks, max_offset + h + 200, w)
    
            self.result = ScanResult(long_img=long_img, blocks=blocks,
                                     questions=questions, max_offset=max_offset,
                                     px_per_press=ppp, frames=frames)
            self.result.scanner = self
            # 扫描结束页面停在底部;预缓存长图灰度并初始化偏移先验,
            # 作答阶段首轮 current_offset 直接窗口命中免全范围搜索
            self._cache_long_gray()
            self._offset_prior = max_offset
            self._presses_delta = 0
            logger.info(f"整页解析完成:共 {len(questions)} 题"
                        f"(完整 {sum(1 for q in questions if q.is_answerable)} 题)")
            return self.result
        finally:
            # 中途异常/停止也需等在途 OCR 结束再退出线程池(最多一帧时长)
            ocr_pool.shutdown(wait=True)
    
    def _merge_blocks(self, frames: list[tuple[int, Image.Image]],
                      frame_blocks: list | None = None) -> list[OcrBlock]:
        """逐帧 OCR 并合并到长图坐标系。
        每帧丢弃"帧顶重叠区内"的块(上一帧已完整覆盖:该块在上一帧中
        离底边 >= 50px,检出可靠),既避免重复也避免接缝漏块。
        :param frame_blocks: 扫描阶段后台线程已算好的逐帧 OCR 结果
            (与 frames 等长同序);缺省时回退到串行逐帧 OCR(兼容旧调用)。"""
        merged: list[OcrBlock] = []
        h = frames[0][1].size[1]
        for i, (off, img) in enumerate(frames):
            blocks = frame_blocks[i] if frame_blocks is not None \
                else self.ocr.run(img)
            if i == 0:
                skip_above = -1
            else:
                prev_off = frames[i - 1][0]
                overlap = h - (off - prev_off)     # 本帧与上一帧的重叠高度
                skip_above = overlap - 50          # 上沿 50px 边界带两帧都不保,归本帧
            for b in blocks:
                y = b.box[1]
                if y < skip_above:
                    continue
                merged.append(OcrBlock(
                    text=b.text,
                    box=(b.box[0], b.box[1] + off, b.box[2], b.box[3] + off),
                    confidence=b.confidence))
            logger.info(f"帧{i + 1}/{len(frames)} OCR:{len(blocks)} 块")
        merged.sort(key=lambda b: (b.box[1], b.box[0]))
        return merged

    # ==================== 对齐测量 ====================

    @staticmethod
    def _page_static(img1: Image.Image, img2: Image.Image) -> bool:
        """两帧内容区几乎无差异(页面没动)。用于条带对齐失败时区分
        "到底了(拖拽后页面纹丝不动)"与"页面变了但测不出位移"。"""
        a1 = np.asarray(img1.convert("L"), dtype=np.int16)
        a2 = np.asarray(img2.convert("L"), dtype=np.int16)
        if a1.shape != a2.shape:
            return False
        h, w = a1.shape
        y0, y1 = int(h * 0.15), int(h * 0.9)
        x1, x2 = int(w * 0.1), int(w * 0.8)
        diff = np.abs(a1[y0:y1, x1:x2] - a2[y0:y1, x1:x2]).mean()
        return float(diff) < 2.0

    # 多条带取带位置(占视口高度比例):高条带用于"到岸小位移"场景——
    # 滚到底附近时低位条带已落在页面尾部留白区(无内容可对齐),靠 0.3/0.42
    # 高带捕捉残余的几十像素位移;大位移时高带越界自动跳过,不影响搜索。
    # 带内有墨迹(深色像素>0.5%)才参与对齐。
    # (0.18,0.25) 低带主要服务大步长下的"反向对齐":cur 低条带在 prev
    # 中位于 y0+moved(不受顶部 chrome 带限制),与正向高条带联合投票,
    # 使 0.7 屏大步长下仍有 ≥2 条带参与多数表决
    _STRIP_RATIOS = ((0.18, 0.25), (0.30, 0.37), (0.42, 0.49),
                     (0.55, 0.62), (0.66, 0.73), (0.77, 0.84), (0.88, 0.95))
    # 反向对齐用低条带(cur 帧下部内容在小位移后会移出视口,只有低带
    # 能在 prev 中找到):y1b+moved ≤ h 决定大步长下 0.30/0.42 也可能越界
    _STRIP_RATIOS_LOW = ((0.18, 0.25), (0.30, 0.37), (0.42, 0.49))

    def _content_band(self, w: int, h: int) -> tuple[int, int]:
        """对齐条带的 x 范围:题目内容区(排除左导航与右侧答题卡,
        答题卡若为 fixed 定位不随滚动,会污染对齐信号)。
        x1 优先复用 InputController 已探测的侧边栏右缘(同运行期缓存),
        兜底才用 roi 配置值——避免宽侧边栏机器上 x1 落进侧栏深色区,
        深色像素被判为"有墨迹"把整条侧栏噪声带入对齐。"""
        sidebar_right = self.input._safe_click_column() if self.input else None
        if sidebar_right is not None:
            x1 = max(sidebar_right + 5, int(w * 0.1), self.locator.region[0] + 40)
        else:
            x1 = max(int(w * 0.1), self.locator.region[0] + 40)
        x2 = max(x1 + 100, min(int(w * 0.75), self.locator.content_x2(w) - 30))
        return x1, x2

    def _pick_strips(self, arr: np.ndarray, x1: int, x2: int,
                      ratios=None) -> list:
        """取视口多个有内容的条带 [(y0, band)];全空白返回 [].
        墨迹判定放宽到灰度<220(原<200):学习通题目底部常有浅灰色工具栏
        或判断题"对/错"短选项,其灰度约 200~220,原门槛把这类条带误判
        为空白导致 frame_offset 返回 None,但页面又确实滚动了,触发
        整条带对齐失败的放弃逻辑。放宽后只要条带内有足够深色像素
        (占比 >= 0.3%)就参与对齐。
        :param ratios: 条带位置覆盖(默认 _STRIP_RATIOS 正向全套;
        反向对齐传 _STRIP_RATIOS_LOW 取 cur 低带)。"""
        h = arr.shape[0]
        strips = []
        for r0, r1 in (ratios or self._STRIP_RATIOS):
            y0, y1b = int(h * r0), int(h * r1)
            band = arr[y0:y1b, x1:x2]
            ink_ratio = float((band < 220).mean())
            if ink_ratio >= 0.003:
                strips.append((y0, band.astype(np.float32)))
            else:
                logger.debug(f"条带 y[{y0}-{y1b}] 墨迹占比 {ink_ratio:.3f} "
                             f"<0.3%,跳过")
        return strips

    def _vote_at(self, specs, a_tgt, x1: int, x2: int, off: int):
        """给定偏移 off,统计条带列表在目标图 a_tgt 上的内条投票。
        specs 元素为 (y0, band, subtract) 或旧式 (y0, band)(方向取调用方
        传入的补参——本方法要求 3 元组,旧式由 _register 包装)。
        subtract=True: 目标行 ya = y0 - off(帧向下滚、内容上移);
        subtract=False: ya = y0 + off(反向对齐/视口对齐长图)。
        返回 (n_in, mean_d):内条数与内条平均残差(无内条返回 (0, inf))。"""
        s, n_in = 0.0, 0
        for y0, band, sub in specs:
            ya = y0 - off if sub else y0 + off
            yb = ya + band.shape[0]
            if ya < 0 or yb > a_tgt.shape[0]:
                continue
            d = float(np.abs(band - a_tgt[ya:yb, x1:x2]).mean())
            if d < _STRIP_MATCH_DIFF:
                s += d
                n_in += 1
        return n_in, (s / n_in if n_in else float("inf"))

    def _register(self, strips, a_tgt, x1: int, x2: int,
                  max_off: int, subtract: bool, off_lo: int = 0,
                  min_votes: int = 1):
        """在目标图 a_tgt 上搜索 src 条带的对齐偏移(滑窗步长 1)。
        strips 元素:(y0, band) 或 (y0, band, subtract):
        - 2 元组用全局 subtract:True=ya=y0-off(frame_offset 正向);
          False=ya=y0+off(current_offset 视口对长图);
        - 3 元组自带方向(双向联合搜索时正向/反向条带混装,但注意
          双向两方向的搜索目标图不同,只能各自单独调 _register,
          候选再交给 _vote_at 联合验证)。
        逐偏移统计"同时良好对齐(残差<_STRIP_MATCH_DIFF)的条带数":
        正确偏移让尽量多的条带同时近零残差(同位置截图 bit-exact,实测
        d=0.000);被吸顶分组标题/光标/聚焦框污染的条带残差常>30,在正确
        偏移也无法低残差,不计入内条数——故单条毒带不影响多数表决;错位
        偏移即便"白对白"残差也 >1.5,拿不到内条。
        选择内条数最多的偏移(并列取残差最小)。
        :param off_lo: 搜索窗口下限(先验窗口搜索时只扫 [off_lo, max_off],
            免去全范围步长 1 的 Python 循环)。
        :param min_votes: 命中所需最少内条数。先验窗口搜索传 2:窗口内
            单票弱命中(如黑块边缘部分重叠的假对齐)不可轻信,否则真实
            偏移在窗口外时无恢复手段(实测稀疏测试页踩坑:窗口内唯一
            候选 1 票/残差0.71 vs 真实位置 4 票/残差0.00)。
        返回 (best_off, mean_d);无任何内条或残差超限返回 None。"""
        best = None  # (n_inlier, -mean_d, mean_d, off)
        specs = []
        for spec in strips:
            if len(spec) == 3:
                y0, b, sub = spec
            else:
                y0, b, sub = spec[0], spec[1], subtract
            specs.append((y0, b.astype(np.int16), sub))
        for off in range(max(0, off_lo), max_off + 1):
            n_in, mean_d = self._vote_at(specs, a_tgt, x1, x2, off)
            if n_in >= min_votes:
                key = (n_in, -mean_d)
                if best is None or key > best[0]:
                    best = (key, mean_d, off)
        if best is None or best[1] > _ALIGN_DIFF_LIMIT:
            return None
        return best[2], best[1]

    def frame_offset(self, img_prev: Image.Image, img_cur: Image.Image) -> int | None:
        """测量 img_cur 相对 img_prev 向下滚动的像素数(双向条带对齐)。
        正向:prev 条带(见 _STRIP_RATIOS)在 cur 中搜索(ya = y0 - off,
        内容随滚动上移);反向:cur 低条带(见 _STRIP_RATIOS_LOW)在 prev
        中搜索(ya = y0 + off)。大步长(≥0.6 屏)下正向高条带滚到接近
        cur 顶部固定 chrome 带(y < _CHROME_H)而对不上内容,反向低条带
        恰能补上——两方向各自多数表决后,对候选偏移做双向联合验证
        (总内条数最多者胜)。off=0 参与(0=未移动)。
        返回 None 表示无条带能可靠测量(调用方应放弃而非当作到底)。
    
        条带对齐失败时会做两次兑底:
        a) 估算 off = 最近一批按键数 × ppp(ppp 从扫描中实时值/结果/标称
           取),对正向+反向条带做单点联合验证;
        b) 全帧降采样(1/4)滑窗搜索最小残差位置,再放大回原分辨率验证。
        兑底失败才真正返回 None。"""
        a1 = np.asarray(img_prev.convert("L"), dtype=np.int16)
        a2 = np.asarray(img_cur.convert("L"), dtype=np.int16)
        h, w = a1.shape
        x1, x2 = self._content_band(w, h)
        strips_pos = self._pick_strips(a1, x1, x2)
        strips_neg = self._pick_strips(a2, x1, x2, self._STRIP_RATIOS_LOW)
        # 3 元组自带方向标志:True=正向(ya=y0-off,查 cur);False=反向
        # (ya=y0+off,查 prev)。两方向目标图不同,分别搜索后联合验证。
        pos_specs = [(y0, b.astype(np.int16), True) for y0, b in strips_pos]
        neg_specs = [(y0, b.astype(np.int16), False) for y0, b in strips_neg]
        logger.info(f"[align] 条带对齐: x=[{x1},{x2}], "
                    f"正向 {len(pos_specs)}/{len(self._STRIP_RATIOS)} 条带, "
                    f"反向 {len(neg_specs)}/{len(self._STRIP_RATIOS_LOW)} 条带, "
                    f"视口 {w}x{h}")
        reg_pos = reg_neg = None
        if pos_specs:
            reg_pos = self._register(pos_specs, a2, x1, x2, h - 60,
                                     subtract=True)
        if neg_specs:
            reg_neg = self._register(neg_specs, a1, x1, x2, h - 60,
                                     subtract=False)
    
        # 双向候选联合验证:同一 off 在两方向同时数内条,取总数最多者
        cands = [reg[0] for reg in (reg_pos, reg_neg) if reg is not None]
        if cands:
            best = None   # ((n_total, -mean_d), mean_d, off, n_pos, n_neg)
            for off in dict.fromkeys(cands):
                n1, d1 = self._vote_at(pos_specs, a2, x1, x2, off)
                n2, d2 = self._vote_at(neg_specs, a1, x1, x2, off)
                # 无内条方向的 (0, inf) 不参与累加:inf*0=nan 会污染均值,
                # 使正确偏移通不过残差校验而落入兜底(稀疏页面实测踩坑)
                n = n1 + n2
                s = (d1 * n1 if n1 else 0.0) + (d2 * n2 if n2 else 0.0)
                mean_d = s / n if n else float("inf")
                if n:
                    key = (n, -mean_d)
                    if best is None or key > best[0]:
                        best = (key, mean_d, off, n1, n2)
            if best is not None and best[1] <= _ALIGN_DIFF_LIMIT:
                logger.info(f"[align] 双向条带命中 off={best[2]} "
                            f"(正{best[3]}/{len(pos_specs)}+反{best[4]}/{len(neg_specs)}票, "
                            f"mean_diff={best[1]:.2f})")
                return best[2]
            logger.info("[align] 双向条带无法达成共识(残差超限),进入兜底")
        else:
            logger.info("[align] 正反向均无有效条带,进入兜底流程")

        # ---- 兜底 a): 按估算偏移做双向单点验证 ----
        # 按键数优先用扫描/回卷时记录的最近一批(旧固定 8 与大步长不符);
        # ppp 依次取扫描中实时值 → 扫描结果实测值 → 标称 40
        ppp = self._ppp_live or (self.result.px_per_press
                                 if self.result and self.result.px_per_press
                                 else None) or _PRESS_PX
        est_off = int(max(0, min(self._last_presses * ppp, h - 60)))
        if pos_specs or neg_specs:
            n1, d1 = self._vote_at(pos_specs, a2, x1, x2, est_off)
            n2, d2 = self._vote_at(neg_specs, a1, x1, x2, est_off)
            # 同联合验证:无内条方向不累加,防 inf*0=nan
            n_in = n1 + n2
            s = (d1 * n1 if n1 else 0.0) + (d2 * n2 if n2 else 0.0)
            if n_in and (s / n_in) < _ALIGN_DIFF_LIMIT:
                logger.info(f"条带对齐失败,兑底估算 off={est_off} "
                            f"(n_in={n_in}) 验证通过")
                return est_off

        # ---- 兜底 b): 全帧降采样滑窗(跳过顶部固定 chrome 带) ----
        logger.info(f"条带对齐全失败,启动全帧降采样兜底 (est_off={est_off})")
        return self._fallback_offset(a1, a2, x1, x2, h)

    def _fallback_offset(self, a1: np.ndarray, a2: np.ndarray,
                         x1: int, x2: int, h: int) -> int | None:
        """全帧降采样滑窗兜底对齐:把 prev/cur 灰度图按 1/4 分辨率降采样,
        在 cur 上滑窗搜索 prev 下半部分的最小残差位置,再放大回原分辨率
        做 ±4px 微调,验证通过才返回。比条带对齐慢但覆盖面更广,哪怕
        所有条带都没墨迹(极稀疏内容)也能测出位移。"""
        scale = 4
        bh = max(6, _CHROME_H // scale)
        # 跳过顶部 chrome 带;prev 只取下半部分(上半部分滚出视口)
        half = (h - bh) // 2
        sub1 = a1[bh + half:h, x1:x2]
        sub2 = a2[bh:, x1:x2]
        src = sub1[::scale, ::scale].astype(np.float32)
        tgt = sub2[::scale, ::scale].astype(np.float32)
        # 全白底(src 均值>250)没有可对齐的内容,直接放弃
        if src.mean() > 250:
            return None
        sh, sw = src.shape
        th = tgt.shape[0]
        if sh < 20 or sw < 30 or th < sh + 10:
            return None
        max_d_off = min(h - 60, th * scale - 10)
        best_off, best_d = -1, float("inf")
        # 滑窗:off 含义=prev 向下滚的原像素数,src 在降采样 tgt 上出现
        # 于 ya = (bh + half - off) // scale 处(src 是 prev[bh+half:...]
        # 的降采样,tgt 是 cur[bh:...] 的降采样)
        ya_base = (bh + half) // scale
        for off in range(0, min(max_d_off + 1, (th - sh) * scale), scale * 2):
            ya = ya_base - off // scale
            if ya < 0 or ya + sh > th:
                continue
            d = float(np.abs(src - tgt[ya:ya + sh]).mean())
            if d < best_d:
                best_d, best_off = d, off
        if best_off < 0:
            return None
        # 原分辨率微调 ±scale
        fine_off, fine_d = best_off, best_d
        src_sub = a1[bh + half:bh + half + half, x1:x2].astype(np.float32)
        for delta in range(-scale, scale + 1):
            off_try = best_off + delta
            if not (0 <= off_try <= h - 60):
                continue
            ya = bh + half - off_try
            yb = ya + half
            if ya < 0 or yb > a2.shape[0]:
                continue
            d = float(np.abs(
                src_sub - a2[ya:yb, x1:x2].astype(np.float32)).mean())
            if d < fine_d:
                fine_d, fine_off = d, off_try
        if fine_d < _ALIGN_DIFF_LIMIT + 6:
            logger.info(f"全帧兜底对齐成功 off={fine_off}, "
                        f"mean_diff={fine_d:.2f}")
            return fine_off
        logger.info(f"全帧兜底对齐失败 mean_diff={fine_d:.2f}")
        return None

    def _cache_long_gray(self):
        """缓存长图灰度数组(长图上万行,每次 convert("L") 数百毫秒,
        作答阶段每题 scroll_to 都要对齐 1~2 次,缓存后零成本)。"""
        res = self.result
        if res is None or res.long_img is not self._lng_src:
            self._lng_src = res.long_img if res else None
            self._lng_gray = (np.asarray(res.long_img.convert("L"), dtype=np.int16)
                              if res else None)

    def _note_presses(self, presses: int):
        """记录一次方向键滚动,维护先验位移估计(current_offset 窗口搜索用)。"""
        ppp = self._ppp_live or (self.result.px_per_press
                                 if self.result and self.result.px_per_press
                                 else None) or _PRESS_PX
        self._presses_delta += presses * ppp
        self._last_presses = abs(presses)

    def _note_home(self):
        """Home 跳顶后页面回到偏移 0,先验直接归零(免窗口搜索扑空)。"""
        self._offset_prior = 0
        self._presses_delta = 0

    def _coarse_offset(self, cur: np.ndarray, lng: np.ndarray,
                       x1: int, x2: int) -> int | None:
        """降采样(1/4)全范围粗搜当前视口在长图中的偏移(无先验/窗口
        扑空时的兑底,思路同 _fallback_offset 的粗搜+精搜):
        取视口中段内容块降采样后在长图降采样图上滑窗(步长 8px),
        最优位置回原分辨率 ±8 精搜由调用方用 _register 完成。
        返回粗偏移或 None(无可对齐内容)。"""
        scale = 4
        h = cur.shape[0]
        # 视口中段(避开顶部 chrome 带、底部窄留白);src/tgt 都切同一
        # 列段 [x1,x2) 再降采样,列对齐才能逐元素求残差(直接整图降采样
        # 会因宽度不同广播失败,实测踩坑)
        y0, y1 = max(_CHROME_H, int(h * 0.15)), int(h * 0.85)
        src = cur[y0:y1, x1:x2][::scale, ::scale].astype(np.float32)
        if src.size == 0 or src.mean() > 250:
            return None                      # 全白底无可对齐内容
        tgt = lng[:, x1:x2][::scale, ::scale].astype(np.float32)
        # 降采样整除误差可能让两图差一列,统一到较窄者
        if tgt.shape[1] != src.shape[1]:
            wmin = min(tgt.shape[1], src.shape[1])
            src, tgt = src[:, :wmin], tgt[:, :wmin]
        sh, sw = src.shape
        th = tgt.shape[0]
        if sh < 8 or sw < 10:
            return None
        # 滑窗:off 为原像素偏移;src 行 i 对应长图行 y0+off+4i,
        # 降采样图上行 (y0+off)/4+i(整除误差 ≤3px,粗搜无碍)
        max_off = min(lng.shape[0] - h // 2, th * scale - sh * scale)
        best_off, best_d = -1, float("inf")
        for off in range(0, max_off + 1, scale * 2):
            ya = (y0 + off) // scale
            if ya + sh > th:
                continue
            d = float(np.abs(src - tgt[ya:ya + sh]).mean())
            if d < best_d:
                best_d, best_off = d, off
        if best_off < 0:
            return None
        logger.info(f"[align] 粗搜命中 off≈{best_off} (降采样残差 {best_d:.2f})")
        return best_off

    def current_offset(self, img: Image.Image) -> int:
        """把当前视口对齐到长图,返回当前滚动偏移(无法对齐返回 -1)。
        用当前视口多条带(上/中/下五档)在长图上做多数表决投票
        (_register,subtract=False):条带已排除答题卡/侧栏;吸顶分组标题
        可能污染最上方一档,该带投不出有效票会被自动丢弃,不影响其余各带。
        多档覆盖避免页面底部/顶部只剩窄内容(如判断题对/错小块)时中部
        无墨迹可对齐。

        性能:旧实现每次全范围(lng 高-半屏,长页 12000+)步长 1 搜索,
        是作答阶段最大单点热点。现按三级递进:
        1) 先验窗口:上次实测偏移 + 按键位移估计,只搜 ±_PRIOR_WINDOW;
        2) 降采样粗搜(1/4,全范围) + ±8 原分辨率精搜;
        3) 原全范围步长 1 搜索(最后防线,兼容旧行为)。
        长图灰度数组缓存复用,不再每次 convert。"""
        res = self.result
        if res is None:
            return -1
        cur = np.asarray(img.convert("L"), dtype=np.int16)
        if res.long_img is not self._lng_src:
            self._cache_long_gray()
        lng = self._lng_gray
        h, w = cur.shape
        x1, x2 = self._content_band(w, h)
        strips = []
        for r0, r1 in ((0.10, 0.22), (0.26, 0.38), (0.42, 0.54),
                       (0.58, 0.70), (0.74, 0.86)):
            y0, y1b = int(h * r0), int(h * r1)
            band = cur[y0:y1b, x1:x2]
            if float((band < 200).mean()) >= 0.005:
                strips.append((y0, band.astype(np.float32)))
        if not strips:
            return -1
        max_off = lng.shape[0] - h // 2
        reg = None
        # 1) 先验窗口精搜(常态路径:按键位移估计误差远小于窗口半径)。
        #    命中要求 ≥2 票共识:窗口内单票弱命中不可轻信(真实偏移在
        #    窗口外时,单票假对齐会让定位错得无声无息);宁走粗搜兑底
        #    (全范围信息,代价仅为降采样滑窗)也不冒错定位的风险
        if self._offset_prior is not None:
            guess = int(round(self._offset_prior + self._presses_delta))
            lo = max(0, guess - _PRIOR_WINDOW)
            hi = min(max_off, guess + _PRIOR_WINDOW)
            if hi >= lo:
                reg = self._register(strips, lng, x1, x2,
                                     hi, subtract=False, off_lo=lo,
                                     min_votes=2)
            if reg is not None:
                logger.info(f"[align] 先验窗口命中 off={reg[0]} "
                            f"(先验≈{guess}, mean_diff={reg[1]:.2f})")
        # 2) 粗搜兑底:窗口扑空(点击引起的自动滚动/手动干预使先验失效)
        if reg is None:
            coarse = self._coarse_offset(cur, lng, x1, x2)
            if coarse is not None:
                lo = max(0, coarse - 8)
                hi = min(max_off, coarse + 8)
                if hi >= lo:
                    reg = self._register(strips, lng, x1, x2,
                                         hi, subtract=False, off_lo=lo)
                if reg is not None:
                    logger.info(f"[align] 粗搜精搜命中 off={reg[0]} "
                                f"(粗搜≈{coarse}, mean_diff={reg[1]:.2f})")
        # 3) 最后防线:全范围步长 1(与旧实现一致,防窗口/粗搜双重失效)
        if reg is None:
            reg = self._register(strips, lng, x1, x2,
                                 max_off, subtract=False)
        if reg is None:
            return -1
        off, d = reg
        if d > _ALIGN_DIFF_LIMIT + 6:
            return -1
        # 测量成功:更新先验,后续窗口搜索以此为新锚点
        self._offset_prior = off
        self._presses_delta = 0
        return off

    # ==================== 定位滚动 ====================

    def scroll_to(self, target: int) -> int:
        """滚动到目标偏移(页面绝对坐标系的滚动量),返回实测偏移。
        按方向键(每格约 40px)逼近,每轮后对齐实测校正,最多迭代 4 轮;
        target<=20 时直接 Home。
        容差 30px:40px 步进下最坏残差 20px,而点击坐标由返回的实测
        偏移换算,精度不受容差影响。"""
        res = self.result
        if res is None:
            return -1
        target = max(0, min(int(target), res.max_offset))
        if target <= 20:
            self.input.press_home()
            self._note_home()
            time.sleep(self.cfg["action"].get("page_wait", 1.0))
            return self.current_offset(self.window.screenshot())

        settle = float(self.cfg["action"].get("scan_settle", 0.7)) * 0.7
        measured = -1
        for attempt in range(4):
            self._check_stop()
            measured = self.current_offset(self.window.screenshot())
            if measured < 0:
                logger.warning("当前视口无法对齐到长图(内容可能已变化)")
                return -1
            if abs(measured - target) <= 30:
                return measured
            presses = int(round((target - measured)
                                / (res.px_per_press or _PRESS_PX)))
            if presses == 0:
                presses = 1 if target > measured else -1
            if presses > 0:
                self.input.arrow_down(presses)
                self._note_presses(presses)
            else:
                # 向上回卷:比较"逐键↑"与"Home跳顶+向下微调"的按键数,
                # 取更省的路径。整页扫描结束在页底,回第一题作答时
                # 逐键↑需 300+ 次(每键~0.1s,耗时 40s+),Home 一步到顶
                # 后通常只需几键向下微调。
                presses_up = -presses
                presses_via_home = int(round(
                    target / (res.px_per_press or _PRESS_PX)))
                if presses_via_home + 5 < presses_up:
                    logger.info(f"回卷距离大(↑×{presses_up}),"
                                f"改用 Home 跳顶+向下×{presses_via_home}")
                    self.input.press_home()
                    self._note_home()
                    time.sleep(self.cfg["action"].get("page_wait", 1.0))
                    # Home 后页面在顶部(偏移≈0),直接按目标距离的估算
                    # 键数下滚,免得空耗一轮测量迭代(4 轮上限内留更多
                    # 校正机会);真实落点仍由下一轮 current_offset 实测
                    # 校正(px_per_press 估算误差在容差内自然消化)
                    if presses_via_home > 0:
                        self.input.arrow_down(presses_via_home)
                        self._note_presses(presses_via_home)
                else:
                    self.input.arrow_up(presses_up)
                    self._note_presses(-presses_up)
            time.sleep(settle)
        final = self.current_offset(self.window.screenshot())
        return final if final >= 0 else measured
