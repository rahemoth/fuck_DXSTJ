# -*- coding: utf-8 -*-
"""测试:整页扫描器(scanner)+ 批量作答流程(executor)

用伪造的 window/input/ocr 模拟一张"可滚动作业页":
- FakeWindow 按滚动位置渲染视口(白底 + 文本行黑块),截图带 .scroll 标记
- FakeInput 方向键:每按一次 ↓/↑ 页面滚 40px(模拟学习通键盘滚动)
- FakeOcr 按截图的 .scroll 返回该视口可见的文本块(真实 locator 解析)

覆盖:相邻帧对齐测偏移 / 接缝合并不重不漏 / 整页扫描端到端 /
长图→视口坐标换算 / 批量作答流程(dry-run)。

fake 与真实世界的一致性约定:
- OCR 视口底边 50px 内的块会漏检(条带拼接的 skip_above=重叠-50 依赖此
  假设:上一帧离底边 >=50px 的块可靠,否则由本帧补)→ FakeOcr 只返回
  vy <= H-51 的块,保证合并结果不重不漏;
- 方向键每按一次页面滚 40px,clamp 到 [0, PAGE_H-H](到底再按不动)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image, ImageDraw

from core.config import build_config
from core.pipeline.scanner import PageScanner
from core.vision.locator import Question, QuestionLocator
from core.vision.ocr import OcrBlock

W, H = 1200, 800
PRESS_PX = 40                # 每按一次方向键滚动像素(实测学习通)
PAGE_H = 1800                # 页面总高 → max_offset = 1000

# 页面文本行(页面绝对坐标):(文本, x, y)
LINES = [
    ("1. (单选题)下列属于不可变数据类型的是()。", 162, 100),
    ("List", 213, 200), ("Set", 212, 250),
    ("Dictionary", 208, 300), ("Tuple", 209, 350),
    ("2. (单选题) 字符串str='Picture',则str[1:3]的结果是()。", 163, 600),
    ("'Pi'", 212, 700), ("'P'", 212, 750), ("'Pi", 212, 800),
    ("'Picture'", 212, 850),
    ("3. (判断题)Python是一种编译型语言。", 163, 1200),
    ("对", 213, 1300), ("错", 213, 1350),
]


class FakeWindow:
    def __init__(self):
        self.scroll = 0

    def screenshot(self):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        for text, x, y in LINES:
            vy = y - self.scroll
            if -30 < vy < H:
                d.rectangle((x, vy, x + len(text) * 12, vy + 20), fill=(30, 30, 30))
        img.scroll = self.scroll          # FakeOcr 据此返回可见块
        return img

    def client_to_screen(self, x, y):
        return x, y


class FakeInput:
    """方向键:每按一次 ↓/↑ 页面滚 PRESS_PX(clamp 到 [0, max_offset])"""
    def __init__(self, window, cfg):
        self.window = window
        self.cfg = cfg
        self.dry_run = False
        self.presses: list[int] = []    # 正=↓次数,负=↑次数
        self.clicks: list[str] = []       # click_options 记录(dry-run 验证)
        self.homes = 0                    # press_home 调用次数

    def _safe_click_column(self) -> int | None:
        """测试替身:不做真实侧边栏探测,返回 None 让 scanner 走
        content_band 的兜底 x1 = max(w*0.1, region[0]+40) 分支"""
        return None

    def press_home(self):
        self.homes += 1
        self.window.scroll = 0

    def arrow_down(self, times=10):
        self.presses.append(times)
        self.window.scroll = max(0, min(PAGE_H - H,
                                        self.window.scroll + times * PRESS_PX))

    def arrow_up(self, times=10):
        self.presses.append(-times)
        self.window.scroll = max(0, min(PAGE_H - H,
                                        self.window.scroll - times * PRESS_PX))

    def click_options(self, centers, labels):
        # 镜像真实 InputController:无效标签跳过不点击
        self.clicks.extend(l for l in labels if l in centers)


class FakeOcr:
    """按截图标记的滚动位置返回可见文本块(客户区坐标)。
    vy > H-51 的块不返回:模拟 OCR 对视口底边 50px 内的块漏检
    (与 _merge_blocks 的接缝规则一致,保证合并不重不漏)。"""
    def __init__(self, threshold=0.55):
        self.threshold = threshold

    def run(self, img, threshold=None):
        scroll = getattr(img, "scroll", 0)
        blocks = []
        for text, x, y in LINES:
            vy = y - scroll
            if 0 <= vy <= H - 51:
                blocks.append(OcrBlock(text=text, box=(x, vy, x + len(text) * 12, vy + 20),
                                       confidence=0.9))
        blocks.sort(key=lambda b: (b.box[1], b.box[0]))
        return blocks


def make_scanner(cfg=None):
    cfg = cfg or build_config({
        "action": {"page_wait": 0, "scan_step_ratio": 0.7, "scan_settle": 0},
    })
    window = FakeWindow()
    return PageScanner(window, FakeOcr(), QuestionLocator(),
                       FakeInput(window, cfg), cfg), window


# ---------------- 对齐测量 ----------------

def test_frame_offset_measures_scroll():
    s, _ = make_scanner()
    a = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(a)
    # 带间隙、宽度变化的非均匀线条(贴近真实文字):连续实心块会形成
    # 平坦对齐高原(条带比内容块矮时整段内移都完美对齐),无法唯一确定
    for i in range(14):
        y = 600 + i * 17
        d.rectangle((300, y, 500 + (i % 5) * 90, y + 5), fill=(20, 20, 20))
    b = Image.new("RGB", (W, H), "white")
    d2 = ImageDraw.Draw(b)
    for i in range(14):     # 同一内容上移 300px
        y = 300 + i * 17
        d2.rectangle((300, y, 500 + (i % 5) * 90, y + 5), fill=(20, 20, 20))
    assert s.frame_offset(a, b) == 300


def test_frame_offset_no_move():
    s, _ = make_scanner()
    a = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(a)
    d.rectangle((300, 640, 900, 700), fill=(20, 20, 20))
    assert s.frame_offset(a, a.copy()) == 0


def test_frame_offset_blank_returns_none():
    """条带全空白(无可对齐内容)返回 None 而非误判偏移"""
    s, _ = make_scanner()
    a = Image.new("RGB", (W, H), "white")
    assert s.frame_offset(a, a.copy()) is None


# ---------------- 整页扫描端到端 ----------------

def test_scan_end_to_end():
    s, window = make_scanner()
    res = s.scan()
    assert res is not None
    assert res.long_img.size == (W, PAGE_H)
    # 页面总高 1800、视口 800 → max_offset = 1000(对齐精度 ±2px)
    assert abs(res.max_offset - 1000) <= 4
    # ppp 实测应接近 40(底部截短的最后一帧不参与 ppp 更新)
    assert res.px_per_press and abs(res.px_per_press - PRESS_PX) < 2
    assert res.scanner is s

    # 合并块:接缝不重不漏——每行文本恰好出现一次
    texts = [b.text for b in res.blocks]
    assert len(texts) == len(LINES)
    for text, _x, _y in LINES:
        assert texts.count(text) == 1, f"{text} 出现 {texts.count(text)} 次"
    # 坐标映射到长图系:题1锚点 y≈100,题3锚点 y≈1200
    b1 = next(b for b in res.blocks if b.text.startswith("1."))
    b3 = next(b for b in res.blocks if b.text.startswith("3."))
    assert abs(b1.box[1] - 100) <= 4
    assert abs(b3.box[1] - 1200) <= 4

    # 解析:3 题,选项齐全
    assert len(res.questions) == 3
    q1, q2, q3 = res.questions
    assert q1.qtype == "single" and list(q1.options) == ["A", "B", "C", "D"]
    assert q2.qtype == "single" and len(q2.options) == 4
    assert q3.qtype == "judge" and "对" in q3.options and "错" in q3.options
    # 选项中心 y 是长图坐标(题1选项A原 y=200)
    assert abs(q1.option_centers["A"][1] - 210) <= 6
    assert q1.is_answerable and q2.is_answerable and q3.is_answerable


def test_scan_bottom_stop():
    """页面短(1.5屏):按到底后停止,不多扫"""
    global PAGE_H
    old = PAGE_H
    PAGE_H = 1200
    try:
        s, window = make_scanner()
        res = s.scan()
        assert res is not None
        assert abs(res.max_offset - 400) <= 4
        assert len(res.frames) >= 2
    finally:
        PAGE_H = old


# ---------------- current_offset / scroll_to ----------------

def test_current_offset_and_scroll_to():
    s, window = make_scanner()
    res = s.scan()
    # 滚到中部
    window.scroll = 500
    img = window.screenshot()
    assert abs(s.current_offset(img) - 500) <= 4
    # 顶部
    window.scroll = 0
    assert abs(s.current_offset(window.screenshot()) - 0) <= 4
    # scroll_to 目标(题目2选项区 y≈750 → 视口 1/3 处);40px 步进,
    # 容差放宽到 ±25(残差 <1 步,点击坐标由实测偏移换算不受影响)
    off = s.scroll_to(700)
    assert abs(off - 700) <= 25
    assert abs(window.scroll - 700) <= 25
    # ScanResult 委托 scroll_to(作答阶段 executor 走 scan.scroll_to)
    off2 = res.scroll_to(200)
    assert abs(off2 - 200) <= 25


def test_scroll_to_home_target():
    s, window = make_scanner()
    res = s.scan()
    window.scroll = 600
    off = s.scroll_to(0)
    assert off <= 4
    assert window.scroll == 0


def test_scroll_to_long_upward_uses_home_shortcut():
    """整页扫描结束停在页底,回第一题作答(target 小但 >20):
    回卷距离大时应走 Home 跳顶 + 向下微调,而不是逐键↑ 300+ 次。
    注意:scan() 自身也会按 Home/↓,断言只看 scroll_to 之后的增量。"""
    s, window = make_scanner()
    res = s.scan()
    window.scroll = res.max_offset          # 页底(1000)
    inp = s.input
    homes0, npress0 = inp.homes, len(inp.presses)
    off = s.scroll_to(100)
    delta = inp.presses[npress0:]
    # Home 被用过,且没有逐键↑(负增量)
    assert inp.homes - homes0 >= 1, "长距离回卷应触发 Home"
    assert all(p > 0 for p in delta), f"不应逐键↑: {delta}"
    # 增量按键数远小于逐键↑路径(1000-100)/40 ≈ 22 次
    assert sum(abs(p) for p in delta) < 10
    # 落点仍在容差内(点击坐标由实测偏移换算,精度不受影响)
    assert abs(off - 100) <= 30
    assert abs(window.scroll - 100) <= 30


def test_scroll_to_short_upward_keeps_arrow_up():
    """回卷距离小(逐键↑更省)时不应绕路 Home:
    scroll=300 → target=200,逐键↑×2 优于 Home+↓×5。
    注意:scan() 自身也会按 Home,断言只看 scroll_to 之后的增量。"""
    s, window = make_scanner()
    s.scan()
    window.scroll = 300
    inp = s.input
    homes0, npress0 = inp.homes, len(inp.presses)
    off = s.scroll_to(200)
    delta = inp.presses[npress0:]
    assert inp.homes - homes0 == 0, "短距离回卷不应触发 Home"
    assert any(p < 0 for p in delta), f"应逐键↑: {delta}"
    assert abs(off - 200) <= 30
    assert abs(window.scroll - 200) <= 30


def test_scroll_to_home_immediately_scrolls_down():
    """改动2:Home 跳顶后应立即按估算键数下滚,不空耗一轮测量迭代。
    旧实现的缺陷:Home 后仅 sleep 等下一轮 current_offset 从 0 起测,
    浪费 4 轮迭代上限中的一轮。用 current_offset 调用计数区分:
    立即下滚 2 轮收敛(首页测量 + 落点确认),空转版需 3 轮。"""
    s, window = make_scanner()
    s.scan()
    window.scroll = 900
    calls = {"n": 0}
    orig = s.current_offset

    def spy(img):
        calls["n"] += 1
        return orig(img)

    s.current_offset = spy
    try:
        off = s.scroll_to(150)
    finally:
        s.current_offset = orig
    # 第1轮:测得 900 → Home+↓4(150/40≈4 键);第2轮:落在 160±容差内返回
    assert calls["n"] <= 2, f"Home 后应立即下滚,不应空耗测量轮: {calls['n']} 轮"
    assert abs(off - 150) <= 30
    assert abs(window.scroll - 150) <= 30


# ---------------- current_offset 先验窗口搜索(改动3) ----------------

def test_current_offset_prior_follows_keypresses():
    """改动3:按键位移估计维护先验——上次实测后按方向键,
    current_offset 在先验 ±200 窗口内直接命中,不触发粗搜兑底
    (用 _coarse_offset 计数验证;scroll_to 内部同样先调
    _note_presses 再测量,行为一致)。"""
    s, window = make_scanner()
    s.scan()
    window.scroll = 500
    assert abs(s.current_offset(window.screenshot()) - 500) <= 4
    # 程序按键:页面真实 +200px,同时告知先验系统(同 scroll_to 内部)
    s.input.arrow_down(5)
    s._note_presses(5)
    called = {"coarse": 0}
    orig = s._coarse_offset

    def spy(cur, lng, x1, x2):
        called["coarse"] += 1
        return orig(cur, lng, x1, x2)

    s._coarse_offset = spy
    try:
        off = s.current_offset(window.screenshot())
    finally:
        s._coarse_offset = orig
    assert abs(off - 700) <= 4
    assert called["coarse"] == 0, "先验有效时不应触发粗搜兑底"


def test_current_offset_recovers_when_prior_invalid():
    """改动3:先验失效(外部大幅跳变,真实偏移在窗口外)时,经降采样
    粗搜兑底仍能恢复正确偏移——窗口化不得失去定位恢复手段。"""
    s, window = make_scanner()
    s.scan()
    window.scroll = 500
    assert abs(s.current_offset(window.screenshot()) - 500) <= 4
    # 跳变 500px:先验=500 → 窗口 [300,700] 不含真实 0;
    # 窗口内单票假对齐(off=550,黑块边缘部分重叠)应被 ≥2 票共识拒绝
    window.scroll = 0
    assert abs(s.current_offset(window.screenshot()) - 0) <= 4


def test_note_home_zeroes_prior():
    """改动2/3联动:Home 跳顶后先验归零,顶部测量在窗口内直接命中
    (免粗搜/全范围搜索)——长距回卷后定位成本不随页长增长。"""
    s, window = make_scanner()
    s.scan()
    window.scroll = 600
    assert abs(s.current_offset(window.screenshot()) - 600) <= 4
    s.input.press_home()
    s._note_home()
    called = {"coarse": 0}
    orig = s._coarse_offset

    def spy(cur, lng, x1, x2):
        called["coarse"] += 1
        return orig(cur, lng, x1, x2)

    s._coarse_offset = spy
    try:
        off = s.current_offset(window.screenshot())
    finally:
        s._coarse_offset = orig
    assert abs(off - 0) <= 4
    assert called["coarse"] == 0


def test_long_gray_cache_reused():
    """改动3:长图灰度数组缓存——同一长图多次 current_offset
    复用同一数组对象,不再重复 convert("L")(长页上万行,数百毫秒/次)。"""
    s, window = make_scanner()
    s.scan()
    g1 = s._lng_gray
    assert g1 is not None
    window.scroll = 300
    s.current_offset(window.screenshot())
    assert s._lng_gray is g1, "同一长图应复用缓存的灰度数组"


# ---------------- 长图 → 视口坐标换算 ----------------

def test_to_viewport_translation():
    from core.pipeline.executor import Executor
    q = Question(number=1, qtype="single", stem="题干xyz")
    q.option_centers = {"A": (200, 500), "B": (200, 600)}
    q.anchor_y2 = 400
    q.region_y2 = 700
    q.blanks = [{"index": 1, "center": (300, 520),
                 "region": (200, 500, 700, 560)}]
    q.editor_center = (400, 650)

    class _Dummy:  # 只为调用 Executor 的坐标换算方法
        _shift_blank = staticmethod(Executor._shift_blank)
        _to_viewport = Executor._to_viewport

    vq = _Dummy._to_viewport(_Dummy(), q, 300)
    assert vq.option_centers == {"A": (200, 200), "B": (200, 300)}
    assert vq.anchor_y2 == 100 and vq.region_y2 == 400
    assert vq.blanks[0]["center"] == (300, 220)
    assert vq.blanks[0]["region"] == (200, 200, 700, 260)
    assert vq.editor_center == (400, 350)
    # 原题目不被修改
    assert q.option_centers["A"] == (200, 500)


# ---------------- 批量作答流程(dry-run) ----------------

class BatchFlowHarness:
    """拼一个最小 Executor:solver 打桩,window/ocr/input 用 fake,
    scanner 由 _run_batch 内部用这些 fake 真实构建(完整走批量流程)。"""

    def __init__(self):
        self.cfg = build_config({
            "window": {"title_keywords": ["x"], "capture_method": "printwindow"},
            "ocr": {"confidence_threshold": 0.55, "retry_threshold": 0.3},
            "llm": {"base_url": "http://localhost:1", "api_key": "k",
                    "model": "m", "timeout": 5, "max_retries": 0,
                    "concurrency": 1},
            "action": {"dry_run": True, "verify_wait": 0},
        })
        self.window = FakeWindow()
        self.ocr = FakeOcr()

    def build(self):
        from core.pipeline.executor import Executor
        ex = Executor.__new__(Executor)   # 跳过 __init__(不建 LLM/OCR 引擎)
        ex.cfg = self.cfg
        ex.emit = lambda e: None
        ex._stop = __import__("threading").Event()
        ex.window = self.window
        ex.ocr = self.ocr
        ex.locator = QuestionLocator()
        ex.input = FakeInput(self.window, self.cfg["action"])
        ex.input.dry_run = True
        ex.solver = self                # solve 打桩
        ex.done_count = 0
        ex.fail_count = 0
        ex._done_keys = set()
        ex._failed_keys = set()
        ex.processed = set()
        return ex

    def solve(self, q):
        if q.qtype == "judge":
            return ["错"]
        return ["A"]


def test_batch_flow_dry_run():
    hz = BatchFlowHarness()
    ex = hz.build()

    ok = ex._run_batch()
    assert ok, "批量模式应成功"
    assert ex.done_count == 3
    assert ex.fail_count == 0
    # 3 题按页面顺序逐一点击了选项:q1=A, q2=A, q3=错
    assert ex.input.clicks == ["A", "A", "错"]
    # 每题作答前都滚动定位过(第一题在首屏按 Home,后续题按方向键)
    assert len(ex.input.presses) >= 1


def test_batch_flow_handles_unanswerable():
    """求解失败的题计入失败,不中断流程"""
    hz = BatchFlowHarness()
    ex = hz.build()
    calls = {"n": 0}

    def solve(q):
        calls["n"] += 1
        if q.number == 2:
            raise RuntimeError("LLM 超时")
        return ["A"]

    hz.solve = solve
    ok = ex._run_batch()
    assert ok
    # 3题:Q2 求解失败;Q1 正常作答;Q3(判断题)拿到无效标签"A"被跳过
    assert calls["n"] == 3
    assert ex.done_count == 2
    assert ex.fail_count == 1
    assert ex.input.clicks == ["A"]
