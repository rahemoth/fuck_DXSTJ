# -*- coding: utf-8 -*-
"""
题目定位模块(参考 MAA 的 ROI + 锚点思想):

真实学习通作业页结构(2026-08 实测):
- 左侧导航栏(x<100)和右侧答题卡(x>1000)需按区域过滤
- 题目锚点形如 "7. (单选题)下列属于..." 的文本块(题号+题型)
- 选项无字母前缀,以缩进区分(x 偏移约 50px),按 y 顺序赋 A/B/C/D
- 判断题选项为 对/错 文本块
- 章节头如 "二.多选题(14分)"、不支持的题型(填空/简答)需排除

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
    r"^\s*(\d+)\s*[.、．]?\s*[（(]\s*(单选题|多选题|判断题|单选|多选|判断)\s*[)）]\s*(.*)"
)
# 无题号锚点(窗口未拉宽时题号被裁剪完全不可见):"(单选题)xxx"
_TYPE_ONLY_RE = re.compile(
    r"^\s*[（(]\s*(单选题|多选题|判断题|单选|多选|判断)\s*[)）]\s*(.*)"
)
# 纯题号块(题号与题型被 OCR 拆成两块):"7." / "8、"
_BARE_NUM_RE = re.compile(r"^\s*(\d+)\s*[.、．]?\s*$")
# 章节头计分括号:"(14分)" / "(共20分,每小题2分)"(用于排除形如"(多选题)(14分)"的伪锚点)
_SCORE_PAREN_RE = re.compile(r"^\s*[（(][^（）()]*\d+[^（）()]*分\s*[)）]")
# 章节头:"二.多选题(14分)" / "单选题(55.2分)"
_SECTION_RE = re.compile(
    r"^\s*[一二三四五六七八九十\d]*\s*[.、．]?\s*(单选题|多选题|判断题|填空题|简答题)\s*[（(].*分"
)
# 任意编号条目(用于跳过不支持的题型,如 "9. (填空题)xxx")
_NUMBERED_RE = re.compile(r"^\d+\s*[.、．]")
# 字母选项(兼容带字母前缀的页面形态):"A." "A、" "A " 开头
_OPTION_RE = re.compile(r"^([A-Fa-f])\s*[.、。:：\s]\s*(.*)")

_QTYPE_MAP = {"单选题": "single", "单选": "single",
              "多选题": "multiple", "多选": "multiple",
              "判断题": "judge", "判断": "judge"}


@dataclass
class Question:
    """结构化题目"""
    number: int | None = None                    # 题号(滚动页模式)
    qtype: str = "single"                        # single / multiple / judge
    stem: str = ""                               # 题干文本
    options: dict[str, str] = field(default_factory=dict)
    option_centers: dict[str, tuple[int, int]] = field(default_factory=dict)  # 选项点击坐标(客户区)
    complete: bool = True                        # 选项采集完整(未被视口裁剪/OCR漏检)
    incomplete_reason: str = ""                  # 完整性校验失败原因(日志诊断用)

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
        return len(self.options) >= 2

    def to_prompt_text(self) -> str:
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
        self.option_indent = self.roi.get("option_indent", 30)
        self.option_max_dx = self.roi.get("option_max_offset_x", 250)

    # ---------- 对外接口 ----------

    def locate_all(self, blocks: list[OcrBlock], page_height: int | None = None) -> list[Question]:
        """从 OCR 文本块中解析当前可见的所有题目(长滚动页模式)。
        :param page_height: 截图高度,用于检测选项是否被视口底部裁剪
        """
        content = self._merge_split_anchors([b for b in blocks if self._in_region(b)])
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
            questions.append(self._build(content, idx, end, number, qtype_str,
                                         stem, page_height))
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

        # 该题区域内的后续块:遇到其他编号条目(下一题,含不支持的题型)即截断;
        # 过滤章节头与忽略词
        rest = []
        for b in blocks[start + 1:end]:
            text = b.text.strip()
            if not text:
                continue
            if _NUMBERED_RE.match(text):
                break
            if _SECTION_RE.match(text):
                continue
            if any(kw in text for kw in self.roi.get("ignore_blocks", [])):
                continue
            rest.append(b)
        rest.sort(key=lambda b: (b.box[1], b.box[0]))

        if qtype == "judge":
            self._collect_judge(q, anchor, rest, page_height)
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
                t = b.text.strip().rstrip(".、。:：")
                if len(t) == 1 and t.upper() in labels:
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
            elif self.option_indent <= first_dx <= self.option_max_dx:
                # 形态3:无字母缩进行
                y1, y2 = line[0].box[1], line[-1].box[3]
                if prev_opt_y is not None and y1 - prev_opt_y < line_gap and prev_label:
                    # 距上一选项行很近:换行续文
                    q.options[prev_label] += line_text
                    last_opt_y2 = y2
                elif next_auto < len(labels):
                    # 跳过已被字母圈识别占用的标签(OCR 漏检中间字母圈时,
                    # 该行文本应补到缺失字母,而非覆盖已有选项)
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

        # ---- 完整性校验 ----
        # 连续性以最终选项标签为准:OCR 常漏检个别字母圈小块(如 A),
        # 但文本行已由无字母缩进形态(形态3)按 y 序补位,标签集合仍连续
        reason = self._check_complete(
            sorted(q.options.keys()), first_opt_y1, last_opt_y2, stem_bottom, page_height
        )
        q.complete = reason is None
        q.incomplete_reason = reason or ""

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
        # 底部裁剪检查
        reason = self._check_complete([], None, last_opt_y2, None, page_height)
        q.complete = reason is None
        q.incomplete_reason = reason or ""

    def _in_region(self, b: OcrBlock) -> bool:
        """过滤左侧导航栏与右侧答题卡区域"""
        x1, y1, x2, y2 = b.box
        rx1, ry1, rx2, ry2 = self.region
        return rx1 <= x1 <= rx2 and ry1 <= y1 <= ry2


def _load_roi() -> dict:
    path = Path(__file__).parent.parent / "resource" / "roi.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
