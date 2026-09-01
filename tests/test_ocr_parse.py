# -*- coding: utf-8 -*-
"""测试:OCR 文本块 → Question 结构解析(locator)

测试数据取自 2026-08 真实学习通作业页 dump:
- 选项无字母前缀,靠缩进(x 偏移约 50px)区分
- 右侧答题卡(x>1000)、左侧导航栏(x<100)需过滤
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.vision.ocr import OcrBlock
from core.vision.locator import QuestionLocator, Question


def make_blocks(items):
    """items: [(text, x, y)] 快捷构造 OCR 块"""
    return [
        OcrBlock(text=t, box=(x, y, x + len(t) * 12, y + 20), confidence=0.9)
        for t, x, y in items
    ]


def test_real_page_unlettered_options():
    """真实页面:无字母选项 + 右侧答题卡 + 左侧导航栏"""
    locator = QuestionLocator()
    blocks = make_blocks([
        # 左侧导航栏(应被过滤)
        ("消息", 34, 140), ("笔记", 31, 186), ("课程", 20, 233), ("设置", 20, 704),
        # 顶部按钮
        ("作业作答", 330, 52), ("暂时保存", 1019, 91), ("提交", 1127, 90),
        # 右侧答题卡(应被过滤)
        ("单选题（55.2分）", 1075, 171), ("6", 1074, 274),
        ("二.多选题(14分)", 1058, 446), ("21", 1072, 498),
        ("三.判断题(25.2分）", 1058, 560), ("26", 1072, 611), ("28", 1173, 611),
        # 题目7 + 无字母选项
        ("7. (单选题)下列属于不可变数据类型的是()。", 162, 369),
        ("List", 213, 428), ("Set", 212, 475),
        ("Dictionary", 208, 520), ("Tuple", 209, 571),
        # 题目8(下一题)
        ("8. (单选题) 字符串str = 'Picture'， 则 str[1:3] 的结果是()。", 163, 658),
        ("'Pi'", 212, 713),
    ])
    questions = locator.locate_all(blocks)
    assert len(questions) == 2

    q7 = questions[0]
    assert q7.number == 7
    assert q7.qtype == "single"
    assert "不可变数据类型" in q7.stem
    assert list(q7.options) == ["A", "B", "C", "D"]
    assert q7.options["A"] == "List"
    assert q7.options["D"] == "Tuple"
    assert q7.is_answerable
    # 选项点击坐标
    assert q7.option_centers["A"] == (213 + len("List") * 12 // 2, 438)

    q8 = questions[1]
    assert q8.number == 8
    assert q8.qtype == "single"
    assert "Picture" in q8.stem
    assert q8.options == {"A": "'Pi'"}
    assert not q8.is_answerable  # 只见到 1 个选项,不可答


def test_real_page_separate_letter_blocks():
    """真实页面形态2:字母圈是独立小块,与选项文本同行但 x 不同"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1. (单选题)", 193, 390),
        ("以下描述中,属于集合特点的是", 194, 395),
        ("A", 207, 421), ("集合中的数据是无序的", 240, 414),
        ("B", 205, 467), ("集合中的数据是可以重复的", 241, 465),
        ("C", 208, 517), ("集合中的数据是严格有序的", 240, 514),
        ("D", 207, 567), ("集合中必须嵌套一个子集合", 240, 561),
        # 右侧答题卡(过滤)
        ("24", 1254, 496), ("二.多选题 (14分）", 1090, 446),
    ])
    questions = locator.locate_all(blocks)
    assert len(questions) == 1
    q = questions[0]
    # 字母圈不应污染题干
    assert q.stem == "以下描述中,属于集合特点的是"
    assert list(q.options) == ["A", "B", "C", "D"]
    assert q.options["A"] == "集合中的数据是无序的"
    assert q.options["D"] == "集合中必须嵌套一个子集合"
    # 点击坐标在选项文本区域
    ax, ay = q.option_centers["A"]
    assert 240 <= ax <= 240 + len("集合中的数据是无序的") * 12
    assert q.is_answerable


def test_lettered_options_compat():
    """兼容带字母前缀的页面形态"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1.(单选题)下列关于操作系统的说法,正确的是", 160, 60),
        ("A. 操作系统是硬件", 160, 120),
        ("B. 操作系统是系统软件", 160, 140),
        ("C. 操作系统是应用软件", 160, 160),
    ])
    questions = locator.locate_all(blocks)
    assert len(questions) == 1
    q = questions[0]
    assert q.options["A"] == "操作系统是硬件"
    assert set(q.option_centers) == {"A", "B", "C"}


def test_judge_question():
    locator = QuestionLocator()
    blocks = make_blocks([
        ("12. (判断题)TCP 是面向连接的协议", 162, 369),
        ("对", 250, 428),
        ("错", 250, 475),
    ])
    questions = locator.locate_all(blocks)
    q = questions[0]
    assert q.qtype == "judge"
    assert "TCP" in q.stem
    assert set(q.options) == {"对", "错"}
    assert q.is_answerable


def test_unsupported_question_type_skipped():
    """填空题等不支持的题型不应污染上一题"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("7. (单选题)题目内容", 162, 369),
        ("甲", 212, 428), ("乙", 212, 475),
        ("9. (填空题)这是一个填空题", 162, 560),
        ("这里是要填的空", 212, 620),
    ])
    questions = locator.locate_all(blocks)
    assert len(questions) == 1
    q = questions[0]
    assert q.options == {"A": "甲", "B": "乙"}
    assert "填空" not in q.stem
    assert q.is_answerable


def test_stem_multiline():
    """题干换行续文(与题干同列)"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("7. (单选题)下面哪个正确", 162, 369),
        ("这是题干的第二行", 163, 400),
        ("甲", 212, 428), ("乙", 212, 475),
    ])
    questions = locator.locate_all(blocks)
    q = questions[0]
    assert q.stem == "下面哪个正确这是题干的第二行"


def test_next_and_submit_button():
    locator = QuestionLocator()
    blocks = make_blocks([
        ("下一题", 900, 700),
        ("提交", 1127, 90),
        ("暂时保存", 1019, 91),
    ])
    nb = locator.find_next_button(blocks)
    assert nb is not None and nb.text == "下一题"
    sb = locator.find_submit_button(blocks)
    assert sb is not None and sb.text == "提交"


def test_no_questions_on_other_page():
    """非答题页(课程列表等)解析不出题目"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("学习通", 400, 10),
        ("课程列表", 200, 60),
        ("我的课程", 210, 100),
    ])
    assert locator.locate_all(blocks) == []


def test_split_number_type_blocks():
    """窄窗口:题号与题型被 OCR 拆成两个同行块("7." + "(单选题)xxx")"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("7.", 162, 369),
        ("(单选题)下列属于不可变数据类型的是()。", 200, 371),
        ("List", 213, 428), ("Set", 212, 475),
        ("Dictionary", 208, 520),
    ])
    questions = locator.locate_all(blocks)
    assert len(questions) == 1
    q = questions[0]
    assert q.number == 7
    assert q.qtype == "single"
    assert "不可变数据类型" in q.stem
    assert list(q.options) == ["A", "B", "C"]
    assert q.is_answerable


def test_anchor_without_number():
    """窄窗口:题号被裁剪完全不可见,仅 "(单选题)xxx" 也能锚定,题号为 None"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("(单选题)下列属于不可变数据类型的是()。", 162, 369),
        ("List", 213, 428), ("Set", 212, 475),
    ])
    questions = locator.locate_all(blocks)
    assert len(questions) == 1
    q = questions[0]
    assert q.number is None
    assert "不可变数据类型" in q.stem
    assert list(q.options) == ["A", "B"]
    assert q.is_answerable


def test_missing_separator_still_anchors():
    """题号后的"."被 OCR 丢失:"8(多选题)xxx" 仍可锚定"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("8(多选题)下列语句正确的是", 162, 369),
        ("甲", 212, 428), ("乙", 212, 475),
    ])
    questions = locator.locate_all(blocks)
    assert len(questions) == 1
    assert questions[0].number == 8
    assert questions[0].qtype == "multiple"


def test_score_header_with_parens_not_anchor():
    """形如 "(多选题)(14分)" 的章节头不应被误认为题目锚点"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("(多选题)(14分)", 162, 200),
        ("(单选题)下列属于不可变数据类型的是()。", 162, 369),
        ("List", 213, 428), ("Set", 212, 475),
    ])
    questions = locator.locate_all(blocks)
    assert len(questions) == 1
    assert "不可变数据类型" in questions[0].stem


def test_question_key_by_stem():
    """题目唯一标识以题干为准:题号误读(12→1)不会与第1题冲突"""
    q1 = Question(number=1, qtype="single", stem="下列属于不可变数据类型的是")
    q2 = Question(number=1, qtype="single", stem="字符串str切片的结果是")
    q3 = Question(number=12, qtype="single", stem="下列属于不可变数据类型的是")
    assert q1.key != q2.key          # 题号相同但题干不同 → 不同题
    assert q1.key == q3.key          # 题号误读但题干相同 → 同一题
    # 题干含空白差异不应影响 key
    q4 = Question(number=12, qtype="single", stem="下列属于不可变 数据类型的是")
    assert q1.key == q4.key
    # 引号类字符 OCR 不稳定('Q" 与 "Q" 互串)不应影响 key
    q5 = Question(number=10, qtype="single", stem="执行word[0]='Q\"会()")
    q6 = Question(number=10, qtype="single", stem="执行word[0]=\"Q\"会()")
    assert q5.key == q6.key
    # 符号噪声(^~*互串)、全半角括号、逗号句号互串(实测Q8/Q12两次识别差异)
    q7 = Question(number=8, qtype="single", stem="字符串^str=‘Picture\"，则*str[1:3] 的结果是（）。")
    q8 = Question(number=8, qtype="single", stem="字符串~str='Picture\"，则~str[1:3]的结果是（）。")
    assert q7.key == q8.key
    q9 = Question(number=12, qtype="single", stem="列表^a=[1.2.3,4,5.6]，执行^a.append(7) 后，列表变为（）。")
    q10 = Question(number=12, qtype="single", stem="列表^a=[1.2,3,4,5.6]，执行~a.append(7)后，列表变为（）。")
    assert q9.key == q10.key
    # 归一化不应把不同题合并:题干文字不同仍是不同 key
    assert q7.key != q9.key


def test_first_letter_block_missed_still_complete():
    """OCR 漏检首选项字母圈(真实dump实测:A 圈丢失,只有 B/C/D):
    文本行由形态3补位为 A,选项标签仍连续 → 应可作答"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("8.(单选题)字符串str='Picture',则 str[1:3] 的结果是()。", 193, 222),
        ("'Pi'", 244, 280),                       # A 的文本(字母圈被 OCR 漏检)
        ("B", 207, 330), ("ic'", 245, 331),
        ("C", 207, 380), ("'ict'", 244, 379),
        ("D", 206, 428), ("'ture'", 243, 427),
    ])
    questions = locator.locate_all(blocks, page_height=750)
    assert len(questions) == 1
    q = questions[0]
    assert list(q.options) == ["A", "B", "C", "D"]
    assert q.options["A"] == "'Pi'"
    assert q.options["D"] == "'ture'"
    assert q.complete
    assert q.is_answerable


def test_middle_letter_block_missed_labels_correct():
    """OCR 漏检中间字母圈(如 B):B 的文本应补位到 B,而非覆盖 A"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1.(单选题)下面哪个正确", 162, 100),
        ("A", 207, 160), ("甲", 245, 161),
        ("乙", 245, 210),                           # B 的文本(字母圈被 OCR 漏检)
        ("C", 207, 260), ("丙", 245, 261),
        ("D", 207, 310), ("丁", 245, 311),
    ])
    questions = locator.locate_all(blocks, page_height=750)
    assert len(questions) == 1
    q = questions[0]
    assert list(q.options) == ["A", "B", "C", "D"]
    assert q.options["A"] == "甲"
    assert q.options["B"] == "乙"
    assert q.options["C"] == "丙"
    assert q.complete
    assert q.is_answerable


def test_incomplete_reason_bottom_crop():
    """选项贴近视口底部 → 不完整,原因记录为底部裁剪"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1.(单选题)下面哪个正确", 162, 600),
        ("A", 207, 660), ("甲", 245, 661),
        ("B", 207, 715), ("乙", 245, 716),          # y2≈736 > 750-60
    ])
    questions = locator.locate_all(blocks, page_height=750)
    q = questions[0]
    assert not q.complete
    assert q.incomplete_reason == "选项贴近视口底部"


def test_incomplete_reason_label_gap():
    """中间选项整块漏检(标签不连续 A,C,D)→ 不完整,原因记录标签不连续"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1.(单选题)下面哪个正确", 162, 100),
        ("A", 207, 160), ("甲", 245, 161),
        ("C", 207, 260), ("丙", 245, 261),
        ("D", 207, 310), ("丁", 245, 311),
    ])
    questions = locator.locate_all(blocks, page_height=750)
    q = questions[0]
    assert not q.complete
    assert "不连续" in q.incomplete_reason


def test_zoom_merged_blocks_must_be_sorted():
    """回归:放大重识别的块合并后必须按 y 重排(实测bug:
    Q6 选项块(单个数字)乱序追加在列表末尾,被按索引划入 Q7 区域,
    导致 Q6 仍空、Q7 带着错误选项去作答)。locate_all 按索引切分题目区域,
    输入块必须 y 有序 —— 本测试固定该契约。"""
    locator = QuestionLocator()
    # 模拟排序后的合并输入:Q6(数字选项) + Q7(英文选项) 同屏
    blocks = make_blocks([
        ("6. (单选题) Python3中的标准数据类型共有()种。", 193, 400),
        ("A", 207, 450), ("4", 242, 451),
        ("B", 207, 500), ("5", 242, 501),
        ("C", 207, 550), ("6", 242, 551),
        ("D", 207, 600), ("7", 242, 601),
        ("7. (单选题)下列属于不可变数据类型的是()。", 193, 660),
        ("List", 242, 715), ("Set", 242, 765),
    ])
    questions = locator.locate_all(blocks, page_height=850)
    assert len(questions) == 2
    q6, q7 = questions
    assert q6.options == {"A": "4", "B": "5", "C": "6", "D": "7"}
    assert q7.options["A"] == "List"
    # 乱序输入(mapped 追加在末尾)会破坏归属 —— 契约:调用方必须排序
    unsorted_blocks = [blocks[0], blocks[9], blocks[10], blocks[11]] + blocks[1:9]
    questions_bad = locator.locate_all(unsorted_blocks, page_height=850)
    q6_bad = questions_bad[0]
    # 演示乱序后果:Q6 区域为空(其选项块索引上属于 Q7 之后)
    assert not q6_bad.options
