# -*- coding: utf-8 -*-
"""
题目定位模块(参考 MAA 的 ROI + 锚点思想):

真实学习通作业页结构(2026-08 实测):
- 左侧导航栏(x<100)和右侧答题卡(x>1000)需按区域过滤
- 题目锚点形如 "7. (单选题)下列属于..." 的文本块(题号+题型)
- 选项无字母前缀,以缩进区分(x 偏移约 50px),按 y 顺序赋 A/B/C/D
- 判断题选项为 对/错 文本块
- 填空题:题干下有 "第N空" 占位标签的输入框(2026-09 实测,标签在
  输入框内左侧,点击坐标=标签右缘+blank_click_dx,同输入行 y)
- 简答题:题干下方是富文本编辑器(工具栏含"段落格式"等,块 x1 比
  题干列右移 >=toolbar_indent 的即工具栏噪声;编辑器点击点=
  题干锚点x+short_answer_click[0], 题干底+short_answer_click[1])
- 章节头如 "二.多选题(14分)"、不支持的题型需排除

锚点与阈值外置在 roi.json,UI 改版只需调配置。
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.log import get_logger
from core.vision.ocr import OcrBlock

logger = get_logger("vision.locator")

# 题目锚点:"7. (单选题)xxx" / "8.(多选题)xxx"(分隔符可缺失,窄窗口下OCR常丢".")
_QUESTION_RE = re.compile(
    r"^\s*(\d+)\s*[.、．]?\s*[（(]\s*(单选题|多选题|判断题|填空题|简答题|单选|多选|判断|填空|简答)\s*[)）]\s*(.*)"
)
# 无题号锚点(窗口未拉宽时题号被裁剪完全不可见):"(单选题)xxx"
_TYPE_ONLY_RE = re.compile(
    r"^\s*[（(]\s*(单选题|多选题|判断题|填空题|简答题|单选|多选|判断|填空|简答)\s*[)）]\s*(.*)"
)
# 纯题号块(题号与题型被 OCR 拆成两块):"7." / "8、"
_BARE_NUM_RE = re.compile(r"^\s*(\d+)\s*[.、．]?\s*$")
# 章节头计分括号:"(14分)" / "(共20分,每小题2分)"(用于排除形如"(多选题)(14分)"的伪锚点)
_SCORE_PAREN_RE = re.compile(r"^\s*[（(][^（）()]*\d+[^（）()]*分\s*[)）]")
# 章节头:"二.多选题(14分)" / "单选题(55.2分)"
_SECTION_RE = re.compile(
    r"^\s*[一二三四五六七八九十\d]*\s*[.、．]?\s*(单选题|多选题|判断题|填空题|简答题)\s*[（(].*分"
)
# 疑似下一题的编号条目:"9. (论述题)xxx"(题型括号紧跟)或裸题号"9."
# (题号与题型拆块且同行合并失败)。注意:题干自身以编号开头(数学题常见,
# 2026-09 填空题实测题干"2.若等差数列..."被旧规则截断,整题题干与输入框
# 全丢)后面直接跟正文,不得截断。
_NUMBERED_ANCHOR_RE = re.compile(r"^\s*\d+\s*[.、．]\s*(?:[（(].*)?$")
# 字母选项(兼容带字母前缀的页面形态):"A." "A、" "A " 开头;
# 分隔符含右括号:OCR 常把字母圈读成 "A)"/"A）"(2026-09 缩小窗口实测)
_OPTION_RE = re.compile(r"^([A-Fa-f])\s*[.、。:：\s）)]\s*(.*)")

_QTYPE_MAP = {"单选题": "single", "单选": "single",
              "多选题": "multiple", "多选": "multiple",
              "判断题": "judge", "判断": "judge",
              "填空题": "fill", "填空": "fill",
              "简答题": "short_answer", "简答": "short_answer"}
# 填空题输入框内左侧的占位标签:"第1空" / "第 2 空"(OCR 可能丢空格)
_BLANK_LABEL_RE = re.compile(r"^第\s*(\d+)\s*空$")


@dataclass
class Question:
    """结构化题目"""
    number: int | None = None                    # 题号(滚动页模式)
    qtype: str = "single"                        # single / multiple / judge / fill / short_answer
    stem: str = ""                               # 题干文本
    options: dict[str, str] = field(default_factory=dict)
    option_centers: dict[str, tuple[int, int]] = field(default_factory=dict)  # 选项点击坐标(客户区)
    blanks: list[dict] = field(default_factory=list)     # 填空题输入框(按 y 序):
    #     [{"index": 空序(1起), "center": 点击坐标, "region": 内容检测区(x1,y1,x2,y2)}]
    editor_center: tuple[int, int] | None = None  # 简答题富文本编辑器点击坐标
    complete: bool = True                        # 选项采集完整(未被视口裁剪/OCR漏检)
    incomplete_reason: str = ""                  # 完整性校验失败原因(日志诊断用)
    anchor_y2: int = 0                           # 锚点块底部 y(选项区裁剪放大重识别用)
    region_y2: int = 0                           # 该题区域底部 y(下一题锚点顶部或视口底)

    @property
    def key(self) -> str:
        """题目唯一标识:题干归一化前30字(仅保留中文/字母/数字,小写)。
        窄窗口下题号可能被裁剪或OCR误读(如12读成1),题号不可作为唯一键;
        标点/符号是OCR噪声重灾区(^~*互串、全半角括号、引号、逗号句号互串),
        全部剔除后同一题的两次识别才能得到相同 key,否则会重复作答甚至反选。"""
        stem = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", self.stem).lower()[:30]
        return stem if stem else f"#{self.number}"

    @property
    def is_answerable(self) -> bool:
        """题干与选项是否完整可靠,可以安全作答"""
        if not self.stem or not self.complete:
            return False
        if self.qtype == "judge":
            return "对" in self.options and "错" in self.options
        if self.qtype == "fill":
            return bool(self.blanks)
        if self.qtype == "short_answer":
            return self.editor_center is not None
        return len(self.options) >= 2

    def to_prompt_text(self) -> str:
        if self.qtype == "fill":
            lines = [f"题型:填空题",
                     f"题干:{self.stem}",
                     f"共 {len(self.blanks)} 个空(按题面顺序)。"]
            return "\n".join(lines)
        if self.qtype == "short_answer":
            # 简答题文风要求(用户需求:大学生做课后作业的口吻,
            # 自然但不刻意口语化)
            return "\n".join([
                "题型:简答题",
                f"题干:{self.stem}",
                "",
                "回答要求(以一名大学生完成课后作业的口吻作答):",
                "- 你是一名大学生,正在做课后作业,用自己的话把问题讲清楚",
                "- 语气自然平实,像真实学生写的作业,但不要过于口语化",
                "- 不用'嘛/吧/说白了/反正/大概就是这些'这类口头禅",
                "- 标点正常使用,允许偶尔不严谨,但不刻意省略或乱用",
                "- 表述顺序自然:可以先给结论再补充理由,不必像范文那样工整",
                "- 不要用'首先/其次/最后/总之'这类模板词,不要分点编号",
                "- 写成一至两个自然段,长度大约120~220字",
                "- 直接给答案内容,不要任何markdown格式和引号包裹",
            ])
        type_names = {"single": "单选题", "multiple": "多选题", "judge": "判断题"}
        lines = [f"题型:{type_names.get(self.qtype, self.qtype)}",
                 f"题干:{self.stem}"]
        if self.qtype == "judge":
            lines.append("选项:对 / 错")
        else:
            for k in sorted(self.options):
                lines.append(f"{k}. {self.options[k]}")
        return "\n".join(lines)

    def __repr__(self):
        return f"Question(#{self.number} {self.qtype}, stem={self.stem[:30]!r}, options={list(self.options)})"


class QuestionLocator:
    def __init__(self, roi: dict | None = None):
        self.roi = roi or _load_roi()
        self.region = self.roi.get("content_region", [100, 0, 1000, 99999])
        self.card_width = self.roi.get("answer_card_width", 190)
        self.option_indent = self.roi.get("option_indent", 30)
        self.option_max_dx = self.roi.get("option_max_offset_x", 250)
        self.blank_click_dx = self.roi.get("blank_click_dx", 100)
        self.blank_region_width = self.roi.get("blank_region_width", 500)
        self.short_answer_click = self.roi.get("short_answer_click", [260, 145])
        self.toolbar_indent = self.roi.get("toolbar_indent", 25)

    # ---------- 对外接口 ----------

    def content_x2(self, page_width: int | None) -> int:
        """内容区有效右边界。答题卡是右锚定的固定宽面板,窗口越窄其左缘
        越靠左(1200宽时左缘x≈1016,窗口不足约1184宽时进入固定 content_region),
        其中的题号按钮/状态文字会并入选项行文本,LLM 据此答错 → 错选。
        给定截图宽度时右界动态收缩为 min(配置右界, 页宽-答题卡宽)。"""
        if not page_width:
            return self.region[2]
        return min(self.region[2],
                   max(self.region[0] + 100, page_width - self.card_width))

    def locate_all(self, blocks: list[OcrBlock], page_height: int | None = None,
                   page_width: int | None = None) -> list[Question]:
        """从 OCR 文本块中解析当前可见的所有题目(长滚动页模式)。
        :param page_height: 截图高度,用于检测选项是否被视口底部裁剪
        :param page_width: 截图宽度,用于把右侧答题卡挡在内容区外
            (见 content_x2;调用方有截图时应始终传入)
        """
        x2 = self.content_x2(page_width)
        content = self._merge_split_anchors(
            [b for b in blocks if self._in_region(b, x2)])
        anchors = []  # (块索引, 题号|None, 题型, 题干)
        for i, b in enumerate(content):
            text = b.text.strip()
            m = _QUESTION_RE.match(text)
            if m:
                anchors.append((i, int(m.group(1)), m.group(2), m.group(3)))
                continue
            t = _TYPE_ONLY_RE.match(text)
            if t and not _SCORE_PAREN_RE.match(t.group(2)):
                # 题号被裁剪完全不可见,仅凭题型括号锚定
                anchors.append((i, None, t.group(1), t.group(2)))

        questions = []
        for k, (idx, number, qtype_str, stem) in enumerate(anchors):
            end = anchors[k + 1][0] if k + 1 < len(anchors) else len(content)
            q = self._build(content, idx, end, number, qtype_str,
                            stem, page_height)
            q.anchor_y2 = content[idx].box[3]
            q.region_y2 = content[end].box[1] if end < len(content) else (page_height or 99999)
            questions.append(q)
        return questions

    def find_next_button(self, blocks: list[OcrBlock]) -> OcrBlock | None:
        """定位'下一题'按钮(单题翻页模式;在全区域中查找,不受 content_region 限制)"""
        for block in blocks:
            text = block.text.strip()
            if any(text == kw for kw in self.roi["next_button"]):
                return block
        return None

    def find_submit_button(self, blocks: list[OcrBlock]) -> OcrBlock | None:
        """定位'提交'按钮"""
        for block in blocks:
            text = block.text.strip()
            if any(text == kw for kw in self.roi["submit_button"]):
                return block
        return None

    # ---------- 内部解析 ----------

    def _merge_split_anchors(self, blocks: list[OcrBlock]) -> list[OcrBlock]:
        """题号与题型被 OCR 拆成两个同行块("7." + "(单选题)xxx")时合并为一块。
        合并块取两块联合包围盒,x1 以题号块为准(与题干/选项缩进列对齐)。"""
        merged: list[OcrBlock] = []
        i = 0
        while i < len(blocks):
            b = blocks[i]
            if i + 1 < len(blocks):
                n, t = blocks[i], blocks[i + 1]
                mn = _BARE_NUM_RE.match(n.text.strip())
                mt = _TYPE_ONLY_RE.match(t.text.strip())
                if (mn and mt
                        and abs(n.center[1] - t.center[1]) < 15
                        and not _SCORE_PAREN_RE.match(mt.group(2))):
                    text = f"{mn.group(1)}. {t.text.strip()}"
                    box = (min(n.box[0], t.box[0]), min(n.box[1], t.box[1]),
                           max(n.box[2], t.box[2]), max(n.box[3], t.box[3]))
                    merged.append(OcrBlock(text=text, box=box,
                                           confidence=min(n.confidence, t.confidence)))
                    i += 2
                    continue
            merged.append(b)
            i += 1
        return merged

    def _build(self, blocks: list[OcrBlock], start: int, end: int, number,
               qtype_str: str, stem_text: str, page_height) -> Question:
        qtype = _QTYPE_MAP.get(qtype_str, "single")
        q = Question(number=number, qtype=qtype, stem=stem_text.strip())
        anchor = blocks[start]

        # 该题区域内的后续块:遇到疑似下一题的编号条目(含不支持的题型)即截断;
        # 过滤章节头与忽略词
        rest = []
        for b in blocks[start + 1:end]:
            text = b.text.strip()
            if not text:
                continue
            if _NUMBERED_ANCHOR_RE.match(text):
                break
            if _SECTION_RE.match(text):
                continue
            if any(kw in text for kw in self.roi.get("ignore_blocks", [])):
                continue
            rest.append(b)
        rest.sort(key=lambda b: (b.box[1], b.box[0]))

        if qtype == "judge":
            self._collect_judge(q, anchor, rest, page_height)
        elif qtype == "fill":
            self._collect_fill(q, anchor, rest, page_height)
        elif qtype == "short_answer":
            self._collect_short_answer(q, anchor, rest, page_height)
        else:
            self._collect_choice(q, anchor, rest, page_height)
        return q

    def _collect_choice(self, q: Question, anchor: OcrBlock, rest: list[OcrBlock],
                        page_height: int | None = None):
        """收集选择题选项。支持三种形态:
        1. 字母圈是独立 OCR 块 + 同行文本块(真实作业页实测形态)
        2. 字母与文本同块("A. xxx")
        3. 完全无字母(按缩进+y顺序自动赋 A/B/C/D)
        同时做完整性校验(字母连续性/题干-选项间距/底部裁剪)。
        """
        labels = self.roi.get("option_labels", list("ABCDEF"))
        line_gap = self.roi.get("option_line_gap", 35)
        stem_parts = [q.stem] if q.stem else []
        stem_bottom = anchor.box[3]          # 题干最后一行的 y2
        first_opt_y1 = None
        last_opt_y2 = None
        next_auto = 0
        prev_opt_y = None                    # 上一选项行 y1(识别换行续文)
        prev_label = None

        for line in self._group_lines(rest):
            line.sort(key=lambda b: b.box[0])
            # 拆分行内字母块与文本块
            letter = None
            letter_block = None
            text_blocks = []
            for b in line:
                # 字母圈偶带噪声后缀("A）"/"A)",2026-09 缩小窗口 zoom 实测),
                # 剔除标点后再判单字符字母
                t = b.text.strip().rstrip(".、。:：）)")
                if len(t) == 1 and t.upper() in labels:
                    if letter is None:
                        # 取最左侧字母块:行内后出现的疑似字母(如选项文本"6"
                        # 被误读成"D")不得覆盖真字母圈
                        letter = t.upper()
                        letter_block = b
                else:
                    text_blocks.append(b)
            line_text = "".join(b.text.strip() for b in text_blocks)
            first_dx = line[0].box[0] - anchor.box[0]

            if letter is not None:
                # 形态1:独立字母圈 + 文本
                label = letter
                if text_blocks:
                    q.options[label] = line_text
                    xs = [b.box[0] for b in text_blocks] + [b.box[2] for b in text_blocks]
                    ys = [b.box[1] for b in text_blocks] + [b.box[3] for b in text_blocks]
                    q.option_centers[label] = ((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2)
                    y1, y2 = min(ys), max(ys)
                else:
                    q.options[label] = ""
                    q.option_centers[label] = letter_block.center
                    y1, y2 = letter_block.box[1], letter_block.box[3]
                first_opt_y1 = y1 if first_opt_y1 is None else first_opt_y1
                last_opt_y2 = y2
                prev_opt_y, prev_label = y1, label
                next_auto = self._advance_auto_label(labels, next_auto, label)
            elif not text_blocks:
                continue
            elif (m2 := _OPTION_RE.match(line_text)) and m2.group(1).upper() in labels:
                # 形态2:字母与文本同块
                label = m2.group(1).upper()
                if label not in q.options:
                    q.options[label] = m2.group(2).strip()
                    q.option_centers[label] = line[0].center
                    first_opt_y1 = line[0].box[1] if first_opt_y1 is None else first_opt_y1
                    last_opt_y2 = line[0].box[3]
                    prev_opt_y, prev_label = line[0].box[1], label
                    next_auto = self._advance_auto_label(labels, next_auto, label)
            elif self.option_indent <= first_dx <= self.option_max_dx:
                # 形态3:无字母缩进行
                y1, y2 = line[0].box[1], line[-1].box[3]
                if prev_opt_y is not None and y1 - prev_opt_y < line_gap and prev_label:
                    # 距上一选项行很近:换行续文
                    q.options[prev_label] += line_text
                    last_opt_y2 = y2
                elif next_auto < len(labels):
                    # 跳过已被字母圈识别占用的标签(OCR 漏检中间字母圈时,
                    # 该行文本应补到缺失字母,而非覆盖已有选项)。
                    # next_auto 已随每个已识别字母标签推进(_advance_auto_label),
                    # 只会向后顺延、绝不回填上方空缺的标签
                    while next_auto < len(labels) and labels[next_auto] in q.options:
                        next_auto += 1
                    if next_auto >= len(labels):
                        continue
                    label = labels[next_auto]
                    next_auto += 1
                    q.options[label] = line_text
                    q.option_centers[label] = line[0].center
                    first_opt_y1 = y1 if first_opt_y1 is None else first_opt_y1
                    last_opt_y2 = y2
                    prev_opt_y, prev_label = y1, label
            elif abs(first_dx) < self.option_indent:
                # 与题干同列:题干续行
                stem_parts.append("".join(b.text.strip() for b in line))
                stem_bottom = max(stem_bottom, line[-1].box[3])
            # dx 超出范围(右侧答题卡等)的行忽略

        q.stem = "".join(stem_parts).strip()
        # 题干截断检查:以 "(" 结尾说明续行/闭合括号被 OCR 漏检(同判断题)
        if q.stem.endswith(("(", "（")):
            q.complete = False
            q.incomplete_reason = "题干疑似被截断"
            return

        # ---- 完整性校验 ----
        # 连续性以最终选项标签为准:OCR 常漏检个别字母圈小块(如 A),
        # 但文本行已由无字母缩进形态(形态3)按 y 序补位,标签集合仍连续
        reason = self._check_complete(
            sorted(q.options.keys()), first_opt_y1, last_opt_y2, stem_bottom, page_height
        )
        if reason is None:
            reason = self._check_row_gaps(q)
        q.complete = reason is None
        q.incomplete_reason = reason or ""

    def _advance_auto_label(self, labels: list[str], next_auto: int, label: str) -> int:
        """某行被字母圈/字母前缀确认为标签 X 后,后续无字母行的自动标签
        必须推进到 X 之后:页面自上而下 A→B→C...,下方的行不可能属于上方
        空缺的标签。实测 bug:首选项 A 整行漏检(B/C 字母圈正常、D 只剩
        文本行)时,D 的文本被回填给空缺的 A,标签恰好凑成连续的
        [A,B,C]骗过完整性校验——执行器跳过题干在上的题先答后题,
        且点击坐标落在末行(错选)。
        注意只在标签序前进时推进:无字母行已在上方补位(如首行字母圈
        漏检但文本行还在)时 next_auto 已越过该标签,不回退。"""
        if label in labels:
            return max(next_auto, labels.index(label) + 1)
        return next_auto

    def _check_row_gaps(self, q: Question) -> str | None:
        """短选项行距均匀性校验。选项全为短文本(≤3字,不可能换行)时,
        相邻选项行距应基本一致;某段行距明显偏大说明中间整行被 OCR 漏检,
        此时按 y 序自动赋的标签已错位(实测单字符数字行漏检后 A 被错位
        两行,补点把已选对的答案改成错的),须触发选项区放大重识别
        (executor 的 zoom 路径)找回漏检行。"""
        if len(q.option_centers) < 2:
            return None
        if any(len(t.strip()) > 3 for t in q.options.values()):
            return None   # 存在长选项(可能换行),行距本身允许不均匀
        ys = sorted(y for _x, y in q.option_centers.values())
        gaps = [b - a for a, b in zip(ys, ys[1:])]
        # 参考行距:多个行距时取最小值(正常行距),并与配置下限结合,
        # 避免个别页字体偏大(实际行距>配置值)时误报
        row_gap = self.roi.get("option_row_gap", 49)
        ref = max(min(gaps), row_gap * 0.8) if len(gaps) >= 2 else row_gap
        if any(g > ref * 1.6 for g in gaps):
            return f"选项行距异常,疑似整行漏检(gaps={gaps})"
        return None

    def _check_complete(self, opt_labels: list[str], first_opt_y1, last_opt_y2,
                        stem_bottom, page_height: int | None) -> str | None:
        """选项采集完整性校验,防止对 OCR 漏检/视口裁剪的题目误作答。
        返回 None 表示完整,否则返回失败原因(日志诊断用)。"""
        # 1. 选项标签必须从 A 开始连续(如只有 C,D 说明上方被裁剪/漏检;
        #    A,B,D 说明中间有选项整块漏检)
        if opt_labels:
            expect = [chr(ord("A") + i) for i in range(len(opt_labels))]
            if opt_labels != expect:
                return f"选项标签不连续:{','.join(opt_labels)}"
        # 2. 第一个选项应紧跟题干(间距过大说明首选项上方有选项被漏检)
        if first_opt_y1 is not None and stem_bottom is not None:
            if first_opt_y1 - stem_bottom > self.roi.get("stem_option_gap", 110):
                return "题干与首选项间距过大"
        # 3. 最后一个选项不能贴近视口底部(下方选项可能被裁剪)
        if page_height is not None and last_opt_y2 is not None:
            if last_opt_y2 > page_height - self.roi.get("bottom_margin", 60):
                return "选项贴近视口底部"
        return None

    @staticmethod
    def _group_lines(blocks: list[OcrBlock]) -> list[list[OcrBlock]]:
        """把 y 坐标接近的块分为同一行(字母圈与选项文本通常 y 略有偏差)"""
        lines: list[list[OcrBlock]] = []
        for b in sorted(blocks, key=lambda b: (b.box[1], b.box[0])):
            if lines and abs(b.center[1] - lines[-1][0].center[1]) < 15:
                lines[-1].append(b)
            else:
                lines.append([b])
        return lines

    def _collect_judge(self, q: Question, anchor: OcrBlock, rest: list[OcrBlock],
                       page_height: int | None = None):
        """收集判断题选项(对/错文本块)"""
        true_kws = self.roi["judge_options"]["true"]
        false_kws = self.roi["judge_options"]["false"]
        stem_parts = [q.stem] if q.stem else []
        last_opt_y2 = None

        for b in rest:
            text = b.text.strip()
            if len(text) == 1 and text.upper() in "ABCDEF":
                # 相邻选择题的字母圈噪声(判断题无字母选项,实测B圈混入题干)
                continue
            dx = b.box[0] - anchor.box[0]
            if 0 <= dx <= self.option_max_dx:
                if text in true_kws and "对" not in q.options:
                    q.options["对"] = text
                    q.option_centers["对"] = b.center
                    last_opt_y2 = max(last_opt_y2 or 0, b.box[3])
                    continue
                if text in false_kws and "错" not in q.options:
                    q.options["错"] = text
                    q.option_centers["错"] = b.center
                    last_opt_y2 = max(last_opt_y2 or 0, b.box[3])
                    continue
            stem_parts.append(text)

        q.stem = "".join(stem_parts).strip()
        # 题干截断检查:以 "(" 结尾说明续行块被 OCR 漏检
        # (实测锚点"31.(判断题) read("的续行漏检后,残缺题干 key 与完整题干不同,
        #  去重失效导致同题重复作答且 LLM 对残题干给出不同答案,反选了已答选项)
        if q.stem.endswith(("(", "（")):
            q.complete = False
            q.incomplete_reason = "题干疑似被截断"
            return
        # 底部裁剪检查
        reason = self._check_complete([], None, last_opt_y2, None, page_height)
        q.complete = reason is None
        q.incomplete_reason = reason or ""

    def _collect_fill(self, q: Question, anchor: OcrBlock, rest: list[OcrBlock],
                      page_height: int | None = None):
        """收集填空题输入框(2026-09 实测布局):
        - 题干在锚点下方,与锚点同列(空位以下划线呈现)
        - 每个空是一个输入框,框内左侧有 "第N空" 浅灰占位标签(独立 OCR 块)
        - 点击点=标签右缘+blank_click_dx(落在输入框内),y=标签中心
        - 内容检测区=标签包围盒向外扩(验证粘贴是否生效的像素统计区)
        空序以 y 顺序为准(题面从上到下),标签里的数字仅作诊断对照。
        重跑场景:已填过的输入框内有答案文本(与标签同行),按行重叠排除,
        不污染题干。"""
        label_blocks = [b for b in rest if _BLANK_LABEL_RE.match(b.text.strip())]
        rows = [b.center[1] for b in label_blocks]   # 各输入行的 y 中心

        stem_parts = [q.stem] if q.stem else []
        last_blank_y2 = None
        for b in label_blocks:
            cx = b.box[2] + self.blank_click_dx
            region = (b.box[0], b.box[1] - 12,
                      b.box[2] + self.blank_region_width, b.box[3] + 12)
            q.blanks.append({"index": len(q.blanks) + 1,
                             "center": (cx, b.center[1]),
                             "region": region})
            last_blank_y2 = max(last_blank_y2 or 0, region[3])

        for b in rest:
            if b in label_blocks:
                continue
            # 已输入的答案文本与所属标签同一输入行(y 相近),排除
            if any(abs(b.center[1] - ry) < 30 for ry in rows):
                continue
            dx = b.box[0] - anchor.box[0]
            if -self.option_indent <= dx <= self.option_max_dx:
                stem_parts.append(b.text.strip())

        q.stem = "".join(stem_parts).strip()
        if q.stem.endswith(("(", "（")):
            q.complete = False
            q.incomplete_reason = "题干疑似被截断"
            return
        if not q.blanks:
            q.complete = False
            q.incomplete_reason = "未识别到填空输入框"
            return
        # 底部裁剪:最后一个输入框贴近视口底,下方可能还有空未露出
        if page_height is not None and last_blank_y2 is not None:
            if last_blank_y2 > page_height - self.roi.get("bottom_margin", 60):
                q.complete = False
                q.incomplete_reason = "输入框贴近视口底部"

    def _collect_short_answer(self, q: Question, anchor: OcrBlock,
                              rest: list[OcrBlock], page_height: int | None = None):
        """收集简答题信息(2026-09 实测布局):
        - 题干在锚点下方同列;题干下方是富文本编辑器
        - 编辑器工具栏("段落格式"/"字体字号"等)的块 x1 比题干列右移
          >=toolbar_indent(CSS padding 特征),据此过滤工具栏噪声
        - 编辑器点击点=(锚点x+dx, 题干底+dy),落在内容区中部(工具栏高约
          58px+内容上边距,dy=145 时安全避开工具栏且在首行下方)"""
        stem_parts = [q.stem] if q.stem else []
        stem_bottom = anchor.box[3]

        for b in rest:
            if b.box[0] - anchor.box[0] >= self.toolbar_indent:
                continue      # 编辑器工具栏/内容区噪声(右移的富文本UI块)
            stem_parts.append(b.text.strip())
            stem_bottom = max(stem_bottom, b.box[3])

        q.stem = "".join(stem_parts).strip()
        if q.stem.endswith(("(", "（")):
            q.complete = False
            q.incomplete_reason = "题干疑似被截断"
            return
        dx, dy = self.short_answer_click
        q.editor_center = (anchor.box[0] + dx, stem_bottom + dy)
        # 点击点须在视口内且离底边有余量(编辑器约230px高,点击点在中部)
        if page_height is not None:
            if q.editor_center[1] > page_height - self.roi.get("bottom_margin", 60):
                q.complete = False
                q.incomplete_reason = "编辑器贴近视口底部"

    def _in_region(self, b: OcrBlock, x2: int | None = None) -> bool:
        """过滤左侧导航栏与右侧答题卡区域(x2 可为动态收缩后的右界)"""
        x1, y1, bx2, y2 = b.box
        rx1, ry1, rx2, ry2 = self.region
        if x2 is not None:
            rx2 = x2
        return rx1 <= x1 <= rx2 and ry1 <= y1 <= ry2


def _load_roi() -> dict:
    path = Path(__file__).parent.parent / "resource" / "roi.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
