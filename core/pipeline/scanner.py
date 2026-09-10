# -*- coding: utf-8 -*-
"""整页扫描器:方向键逐屏滚动截图 → 图像对齐测偏移 → 拼接长图 →
逐帧 OCR 合并 → 全题一次解析(替代旧的"逐题滚动识别")。

滚动机制:学习通页面滚动条被网页 CSS 隐藏、鼠标滚轮被禁用,唯一可靠
手段是键盘方向键(每按一次 ↓ 精确滚 40px,线性)。所有滚动都先点击
内容区空白列建立焦点(input._focus_content)。

坐标体系:所有题目/文本块坐标为长图坐标(页面绝对坐标)。
作答时用 scroll_to 把目标题滚进视口,再换算 viewport_y = long_y - offset。

关键机制:
- 偏移测量:相邻两帧之间在内容区做条带对齐搜索(同 _page_moved 思想,
  但返回具体偏移值),滚动多少像素不依赖按键数的换算精度;
- 接缝合并:每帧只保留"上一帧没完整覆盖"的块(帧顶 y >= 重叠区-50),
  既无重复也不会漏(重叠区上沿的块在上一帧离底边 >=50px,检出可靠);
- px_per_press:每按一次方向键页面滚动的像素数(标称 40),扫描中实测
  累计,scroll_to 据此估算按键数,再以对齐测量校正,最多迭代 4 轮。
"""
import time
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

    # ==================== 扫描 ====================

    def scan(self) -> ScanResult | None:
        """整页扫描。中途异常返回 None(调用方回退旧循环);
        页面无可滚内容时循环 1 轮自然结束(单题翻页页由执行器兜底)。"""
        self._check_stop()
        self.input.press_home()                     # 回顶(内部先点内容区建焦点)
        time.sleep(self.cfg["action"].get("page_wait", 1.0))
        img0 = self.window.screenshot()
        w, h = img0.size          # PIL size = (宽, 高)
        frames: list[tuple[int, Image.Image]] = [(0, img0)]
        offset = 0
        ppp: float | None = None                   # 实测每按键像素
        step_ratio = float(self.cfg["action"].get("scan_step_ratio", 0.7))
        settle = float(self.cfg["action"].get("scan_settle", 0.7))
        zero_streak = 0                            # 连续"无位移"批次数
        max_frames = int(self.cfg["action"].get("max_scan_frames",
                                                _MAX_FRAMES_DEFAULT))
        _MAX_PRESSES = 8                          # 单批按键上限:实测 40px/键
        # → ≤320px,保证最低对齐条带(0.55h≈568)的内容不被滚出视口;
        # 否则大位移后条带全部越界,空白页噪声会把位移误判为 0(假到底)

        while len(frames) < max_frames:
            self._check_stop()
            prev_img = frames[-1][1]
            # 估算按键数:目标页面步长 = step_ratio * h
            want = int(h * step_ratio)
            presses = max(2, min(_MAX_PRESSES,
                                 round(want / (ppp or _PRESS_PX))))
            self.input.arrow_down(presses)
            time.sleep(settle)
            img = self.window.screenshot()
            if img.size != (w, h):
                logger.warning("扫描中窗口尺寸变化,放弃整页扫描")
                return None
            moved = self.frame_offset(prev_img, img)
            if moved is None:
                # 无法条带对齐:区分"页面没动(到底)"与"动了但测不出
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
                self.input.arrow_down(_MAX_PRESSES)
                presses += _MAX_PRESSES
                time.sleep(settle)
                img2 = self.window.screenshot()
                if img2.size != (w, h):
                    return None
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
                moved, img = moved2, img2
            zero_streak = 0
            if moved > h - 80:
                # 跳幅超过一屏会有未覆盖区域,放弃
                logger.warning(f"单次滚动位移过大({moved}px),存在漏采风险,回退逐题模式")
                return None
            offset += moved
            frames.append((offset, img))
            # ppp 更新:仅在实际位移接近按键请求量时更新
            # (页面到底被截短的那一帧 moved 远小于请求量,比例失真)
            requested = presses * (ppp or _PRESS_PX)
            if ppp is None or abs(moved - requested) <= requested * 0.15:
                ppp_new = moved / presses
                ppp = ppp_new if ppp is None else (ppp + ppp_new) / 2
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
        # 跳过带对应的页面行由上一帧的真实内容覆盖(步进 0.7 屏,重叠足够)。
        long_img = Image.new("RGB", (w, max_offset + h), "white")
        for fi, (off, im) in enumerate(frames):
            if fi == 0:
                long_img.paste(im, (0, off))
            else:
                long_img.paste(im.crop((0, _CHROME_H, w, h)),
                               (0, off + _CHROME_H))

        # ---- 逐帧 OCR + 接缝合并 ----
        blocks = self._merge_blocks(frames)
        # 底部裁剪检查放宽:长图含整页,不存在"贴近视口底部"的截断
        questions = self.locator.locate_all(blocks, max_offset + h + 200, w)

        self.result = ScanResult(long_img=long_img, blocks=blocks,
                                 questions=questions, max_offset=max_offset,
                                 px_per_press=ppp, frames=frames)
        self.result.scanner = self
        logger.info(f"整页解析完成:共 {len(questions)} 题"
                    f"(完整 {sum(1 for q in questions if q.is_answerable)} 题)")
        return self.result

    def _merge_blocks(self, frames: list[tuple[int, Image.Image]]) -> list[OcrBlock]:
        """逐帧 OCR 并合并到长图坐标系。
        每帧丢弃"帧顶重叠区内"的块(上一帧已完整覆盖:该块在上一帧中
        离底边 >= 50px,检出可靠),既避免重复也避免接缝漏块。"""
        merged: list[OcrBlock] = []
        h = frames[0][1].size[1]
        for i, (off, img) in enumerate(frames):
            blocks = self.ocr.run(img)
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
    # 带内有墨迹(深色像素>0.5%)才参与对齐
    _STRIP_RATIOS = ((0.30, 0.37), (0.42, 0.49), (0.55, 0.62),
                     (0.66, 0.73), (0.77, 0.84), (0.88, 0.95))

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

    def _pick_strips(self, arr: np.ndarray, x1: int, x2: int) -> list:
        """取视口下部多个有内容的条带 [(y0, band)];全空白返回 [].
        墨迹判定放宽到灰度<220(原<200):学习通题目底部常有浅灰色工具栏
        或判断题"对/错"短选项,其灰度约 200~220,原门槛把这类条带误判
        为空白导致 frame_offset 返回 None,但页面又确实滚动了,触发
        整条带对齐失败的放弃逻辑。放宽后只要条带内有足够深色像素
        (占比 >= 0.3%)就参与对齐。"""
        h = arr.shape[0]
        strips = []
        for r0, r1 in self._STRIP_RATIOS:
            y0, y1b = int(h * r0), int(h * r1)
            band = arr[y0:y1b, x1:x2]
            ink_ratio = float((band < 220).mean())
            if ink_ratio >= 0.003:
                strips.append((y0, band.astype(np.float32)))
            else:
                logger.debug(f"条带 y[{y0}-{y1b}] 墨迹占比 {ink_ratio:.3f} "
                             f"<0.3%,跳过")
        return strips

    def _register(self, strips, a_tgt, x1: int, x2: int,
                  max_off: int, subtract: bool):
        """在目标图 a_tgt 上搜索 src 条带(strips=[(y0,band)])的对齐偏移。
        subtract=True: 目标行 ya = y0 - off(帧向下滚、内容上移,frame_offset);
        subtract=False: ya = y0 + off(视口对齐到长图,current_offset)。
        逐偏移(步长1)统计"同时良好对齐(残差<_STRIP_MATCH_DIFF)的条带数":
        正确偏移让尽量多的条带同时近零残差(同位置截图 bit-exact,实测
        d=0.000);被吸顶分组标题/光标/聚焦框污染的条带残差常>30,在正确
        偏移也无法低残差,不计入内条数——故单条毒带不影响多数表决;错位
        偏移即便"白对白"残差也 >1.5,拿不到内条。
        选择内条数最多的偏移(并列取残差最小)。
        返回 (best_off, mean_d);无任何内条或残差超限返回 None。"""
        best = None  # (n_inlier, -mean_d, mean_d, off)
        bands = [(y0, b.astype(np.int16), b.shape[0]) for y0, b in strips]
        for off in range(0, max_off + 1):
            s, n_in = 0.0, 0
            for y0, band, bh in bands:
                ya = y0 - off if subtract else y0 + off
                yb = ya + bh
                if ya < 0 or yb > a_tgt.shape[0]:
                    continue
                d = float(np.abs(band - a_tgt[ya:yb, x1:x2].astype(np.int16)).mean())
                if d < _STRIP_MATCH_DIFF:
                    s += d
                    n_in += 1
            if n_in:
                mean_d = s / n_in
                key = (n_in, -mean_d)
                if best is None or key > best[0]:
                    best = (key, mean_d, off)
        if best is None or best[1] > _ALIGN_DIFF_LIMIT:
            return None
        return best[2], best[1]

    def frame_offset(self, img_prev: Image.Image, img_cur: Image.Image) -> int | None:
        """测量 img_cur 相对 img_prev 向下滚动的像素数。
        条带取 img_prev 下部多条(见 _STRIP_RATIOS),在 img_cur 全高用
        内条多数表决(_register)求对齐偏移;off=0 参与(0=未移动)。
        返回 None 表示无条带能可靠测量(调用方应放弃而非当作到底)。

        条带对齐失败时会做两次兜底:
        a) 估算 off = presses * ppp(ppp 从配置或实测值取),用 _register
           只搜这一个偏移验证;
        b) 全帧降采样(1/4)滑窗搜索最小残差位置,再放大回原分辨率验证。
        兜底失败才真正返回 None。"""
        a1 = np.asarray(img_prev.convert("L"), dtype=np.int16)
        a2 = np.asarray(img_cur.convert("L"), dtype=np.int16)
        h, w = a1.shape
        x1, x2 = self._content_band(w, h)
        strips = self._pick_strips(a1, x1, x2)
        n_total = len(self._STRIP_RATIOS)
        logger.info(f"[align] 条带对齐: x=[{x1},{x2}], "
                    f"有效条带 {len(strips)}/{n_total}, "
                    f"视口 {w}x{h}")
        if strips:
            reg = self._register(strips, a2, x1, x2, h - 60, subtract=True)
            if reg is not None:
                logger.info(f"[align] 条带直接命中 off={reg[0]}, "
                            f"mean_diff={reg[1]:.2f}")
                return reg[0]
            logger.info(f"[align] {len(strips)} 条带全部无法达成共识(残差超限)")
        else:
            logger.info("[align] 所有条带均无墨迹,进入兜底流程")

        # ---- 兜底 a): 按估算偏移做单点验证 ----
        presses = self.cfg["action"].get("scan_presses", 8)
        est_off = presses * (self.result.px_per_press if self.result
                             and self.result.px_per_press else _PRESS_PX)
        est_off = int(max(0, min(est_off, h - 60)))
        if strips:
            s, n_in = 0.0, 0
            for y0, band, bh in [(y0, b.astype(np.int16), b.shape[0])
                                 for y0, b in strips]:
                ya, yb = y0 - est_off, y0 - est_off + bh
                if 0 <= ya and yb <= a2.shape[0]:
                    d = float(np.abs(band - a2[ya:yb, x1:x2].astype(np.int16)).mean())
                    if d < _STRIP_MATCH_DIFF:
                        s += d; n_in += 1
            if n_in and (s / n_in) < _ALIGN_DIFF_LIMIT:
                logger.info(f"条带对齐失败,兜底估算 off={est_off} "
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

    def current_offset(self, img: Image.Image) -> int:
        """把当前视口对齐到长图,返回当前滚动偏移(无法对齐返回 -1)。
        用当前视口多条带(上/中/下五档)在长图上做中位数共识投票
        (_register,subtract=False):条带已排除答题卡/侧栏;吸顶分组标题
        可能污染最上方一档,该带投不出有效票会被自动丢弃,不影响其余各带。
        多档覆盖避免页面底部/顶部只剩窄内容(如判断题对/错小块)时中部
        无墨迹可对齐。"""
        res = self.result
        if res is None:
            return -1
        cur = np.asarray(img.convert("L"), dtype=np.int16)
        lng = np.asarray(res.long_img.convert("L"), dtype=np.int16)
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
        reg = self._register(strips, lng, x1, x2,
                             lng.shape[0] - h // 2, subtract=False)
        if reg is None:
            return -1
        off, d = reg
        return off if d <= _ALIGN_DIFF_LIMIT + 6 else -1

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
                    time.sleep(self.cfg["action"].get("page_wait", 1.0))
                else:
                    self.input.arrow_up(presses_up)
            time.sleep(settle)
        final = self.current_offset(self.window.screenshot())
        return final if final >= 0 else measured
