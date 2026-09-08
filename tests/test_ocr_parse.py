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
    """不支持的题型(如论述/连线)不应污染上一题"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("7. (单选题)题目内容", 162, 369),
        ("甲", 212, 428), ("乙", 212, 475),
        ("9. (论述题)这是一个论述题", 162, 560),
        ("这里是要写的正文", 212, 620),
    ])
    questions = locator.locate_all(blocks)
    assert len(questions) == 1
    q = questions[0]
    assert q.options == {"A": "甲", "B": "乙"}
    assert "论述" not in q.stem
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


def test_first_option_row_missed_not_backfilled():
    """OCR 漏检首选项整行(A 的字母圈与文本都丢,2026-09 实测"2+2="题,
    0.55 阈值下只检出 B/6、C/8、10 三块):末行文本必须顺延给 D,
    绝不能回填给空缺的 A——那会凑成连续的 [A,B,C] 骗过完整性校验,
    导致执行器跳过题干在上的题先答后题,且点击坐标落到末行(错选)。"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("2. (单选题)2+2=", 435, 656),
        ("B", 450, 786), ("6", 486, 786),
        ("C", 450, 835), ("8", 486, 835),
        ("10", 486, 883),                      # D 的文本(字母圈漏检)
    ])
    questions = locator.locate_all(blocks, page_height=1000)
    assert len(questions) == 1
    q = questions[0]
    assert q.options == {"B": "6", "C": "8", "D": "10"}
    assert not q.complete
    assert "不连续" in q.incomplete_reason
    assert not q.is_answerable


def test_only_tail_text_row_left_not_backfilled():
    """极端形态:首行漏检且只剩最后一个无字母文本行(实测 run 早期只检出
    B/6、10):'10' 与 B 相距约两行距,仍不得回填 A,应顺延给 B 之后,
    标签不连续触发拦截(放大重识别/滚动)而非错误作答"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("2. (单选题)2+2=", 435, 656),
        ("B", 450, 786), ("6", 486, 786),
        ("10", 486, 883),
    ])
    questions = locator.locate_all(blocks, page_height=1000)
    q = questions[0]
    assert q.options == {"B": "6", "C": "10"}
    assert not q.complete
    assert not q.is_answerable


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


def test_judge_letter_noise_filtered():
    """判断题:相邻选择题的字母圈(B)混入区域,不应污染题干"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("31.(判断题) read()函数运行之后，文件指针指向文件末尾。", 193, 200),
        ("对", 242, 260), ("错", 242, 310),
        ("B", 208, 315),                       # 相邻题的字母圈噪声
    ])
    questions = locator.locate_all(blocks, page_height=750)
    q = questions[0]
    assert "B" not in q.stem
    assert q.stem.endswith("文件末尾。")
    assert set(q.options) == {"对", "错"}
    assert q.is_answerable


def test_judge_truncated_stem_incomplete():
    """判断题:续行被 OCR 漏检,题干以"("结尾 → 不完整,不得作答。
    (实测bug:残缺题干 key 与完整题干不同,去重失效导致同题重复作答,
    LLM 对残题干给出不同答案,反选了已答对的选项)"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("31.(判断题) read(", 193, 200),        # 续行块漏检
        ("对", 242, 260), ("错", 242, 310),
    ])
    questions = locator.locate_all(blocks, page_height=750)
    q = questions[0]
    assert not q.complete
    assert q.incomplete_reason == "题干疑似被截断"
    assert not q.is_answerable
    # 完整题干(闭合括号在后)不受影响
    blocks2 = make_blocks([
        ("31.(判断题) read()", 193, 200),
        (")函数运行之后，文件指针指向文件末尾。", 333, 201),
        ("对", 242, 260), ("错", 242, 310),
    ])
    q2 = locator.locate_all(blocks2, page_height=750)[0]
    assert q2.complete
    assert q2.is_answerable


def test_choice_truncated_stem_incomplete():
    """选择题:题干以"("结尾同样视为截断"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("8.(单选题)字符串str='Picture'，则str[1:3]的结果是(", 193, 200),
        ("'Pi'", 244, 260), ("'ic'", 244, 310),
    ])
    questions = locator.locate_all(blocks, page_height=750)
    q = questions[0]
    assert not q.complete
    assert q.incomplete_reason == "题干疑似被截断"


def test_single_char_options_partial_miss():
    """单字符选项部分漏检(Q20实测:选项0/1/None/True只识别出C/D):
    标签不连续应判不完整(触发放大重识别),而非误作答或永久跳过"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("20. (单选题) pandas的read_csv中header参数默认值为()。", 193, 200),
        ("C", 207, 260), ("None", 242, 261),
        ("D", 207, 310), ("True", 242, 311),
    ])
    questions = locator.locate_all(blocks, page_height=750)
    q = questions[0]
    assert list(q.options) == ["C", "D"]
    assert not q.complete
    assert "不连续" in q.incomplete_reason
    assert not q.is_answerable


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


def test_letter_block_with_bracket_noise():
    """字母圈被 OCR 读成 "A）"(带右括号,2026-09 缩小窗口 Q3 放大重识别实测):
    应剔除标点后识别为字母 A;否则 A 行被当题干续行,标签只剩 B/C/D,
    触发不完整拦截——题目在页底时微滚无效,该题将永远无法作答"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("3. (单选题)3+3=", 101, 479),
        ("A）", 112, 557), ("6", 148, 559),
        ("B", 115, 608), ("7", 150, 608),
        ("C ", 115, 657), ("8", 148, 657),
        ("D", 114, 706), ("9", 148, 706),
    ])
    questions = locator.locate_all(blocks, page_height=800)
    assert len(questions) == 1
    q = questions[0]
    assert q.options == {"A": "6", "B": "7", "C": "8", "D": "9"}
    assert q.complete
    assert q.is_answerable


def test_option_re_accepts_bracket_separator():
    """字母与文本同块且分隔符是右括号:"A）1"(缩小窗口 zoom 噪声形态)"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1. (单选题)1+1=", 101, 300),
        ("A）1", 112, 400),
        ("B", 115, 450), ("2", 150, 450),
        ("C", 115, 500), ("3", 150, 500),
        ("D", 114, 550), ("4", 150, 550),
    ])
    questions = locator.locate_all(blocks, page_height=700)
    q = questions[0]
    assert q.options["A"] == "1"
    assert list(q.options) == ["A", "B", "C", "D"]


def test_answer_card_excluded_in_narrow_window():
    """窄窗口答题卡混入(实测1200宽时答题卡左缘x=1016,距固定边界1000仅16px;
    半屏960宽时左缘≈770,深陷内容区):传截图宽度后右界动态收缩,
    答题卡的题号按钮/状态文字不得并入选项文本——否则 LLM 看到"B. 22"
    这类被污染的选项会答错 → 错选"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1. (单选题)1+1=", 102, 338),
        ("A", 114, 417), ("1", 148, 418),
        ("B", 114, 466), ("2", 148, 466),
        ("C", 115, 515), ("3", 148, 514),
        ("D", 114, 564), ("4", 150, 564),
        # 答题卡(960宽窗口左缘≈770):题型头 + 题号按钮与选项行同 y
        ("单选题（100分）", 790, 172),
        ("1", 800, 417), ("2", 800, 466),
        ("已完成", 780, 550),
    ])
    # 不传宽度(旧行为,模拟漏传):答题卡块混入,选项文本被污染
    q_old = locator.locate_all(blocks, page_height=750)[0]
    assert q_old.options["A"] == "11"
    assert q_old.options["B"] == "22"
    # 传入截图宽度:答题卡被挡在内容区外
    q = locator.locate_all(blocks, page_height=750, page_width=960)[0]
    assert q.options == {"A": "1", "B": "2", "C": "3", "D": "4"}
    assert q.complete
    assert q.is_answerable


def test_spurious_letter_block_not_override_real_letter():
    """同行出现两个疑似字母块(Q2 视图实测:字母圈 B@116 与 文本"6"被误读
    成的伪字母 D@154):取最左的真字母圈,误读块不得覆盖——否则 B 标签
    丢失且点击坐标落到误读块位置(错选)"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("2. (单选题)2+2=", 101, 193),
        ("B", 116, 300), ("D", 154, 301),     # B 的字母圈 + "6"误读成的"D"
        ("C", 116, 349), ("8", 152, 349),
        ("D", 116, 397), ("10", 152, 394),
    ])
    questions = locator.locate_all(blocks, page_height=750)
    q = questions[0]
    assert list(q.options) == ["B", "C", "D"]
    assert q.options["C"] == "8" and q.options["D"] == "10"
    # B 的坐标来自真字母圈(x≈116),不是误读块(x≈154)
    assert q.option_centers["B"][0] < 130
    # 标签从 B 开始 → 不连续,触发兜底(放大重识别)而非误作答
    assert not q.complete
    assert not q.is_answerable


# ---------- 填空题 / 简答题(2026-09 布局采集) ----------

def test_fill_question_parsed():
    """填空题:锚点 + 题干(下划线空位) + "第N空"输入框标签。
    布局取自 2026-09 真实作业页(1920全屏)采集:标签在输入框内左侧,
    点击点=标签右缘+blank_click_dx(默认100),y=标签中心。"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("消息", 34, 140),                       # 左侧导航(过滤)
        ("1. (填空题)计算20+99=____,22*5=____", 435, 348),
        ("第1空", 435, 428),
        ("第2空", 435, 623),
    ])
    questions = locator.locate_all(blocks, page_height=1032, page_width=1920)
    assert len(questions) == 1
    q = questions[0]
    assert q.qtype == "fill"
    assert q.number == 1
    assert "20+99" in q.stem and "22*5" in q.stem
    assert len(q.blanks) == 2
    b1, b2 = q.blanks
    assert b1["index"] == 1 and b2["index"] == 2
    # 点击点:标签右缘+100,同输入行;按 y 序编号
    assert b1["center"] == (435 + 3 * 12 + 100, 438)
    assert b2["center"][1] == 633
    # 内容检测区(标签包围盒外扩)
    assert b1["region"] == (435, 416, 471 + 500, 460)
    assert q.complete
    assert q.is_answerable
    # prompt 含空数
    assert "共 2 个空" in q.to_prompt_text()


def test_stem_starting_with_number_not_truncated():
    """回归(2026-09 填空题实测):题干自身以编号开头("2.若等差数列...",
    教材题干自带题号)不得被当作下一题锚点截断——否则该题题干与
    "第N空"输入框标签全部丢失,永远无法作答。"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1. (填空题)", 437, 348),
        ("2.若等差数列an}中,a1=3,d=2,则第10项an=_", 436, 369),
        ("第1空", 435, 419),
        ("2. (填空题)", 436, 539),
        ("双曲线x^2-2y^2=1的焦距为_", 436, 563),
        ("第1空", 435, 614),
    ])
    questions = locator.locate_all(blocks, page_height=1032, page_width=1920)
    assert len(questions) == 2
    q1, q2 = questions
    assert q1.qtype == "fill" and q1.is_answerable
    assert "若等差数列" in q1.stem and "第10项" in q1.stem
    assert len(q1.blanks) == 1
    assert q2.qtype == "fill" and len(q2.blanks) == 1
    assert "双曲线" in q2.stem


def test_fill_question_label_with_spaces():
    """OCR 丢空格形态:"第 1 空" 也能锚定输入框"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1. (填空题)计算20+99=____", 435, 348),
        ("第 1 空", 435, 428),
    ])
    q = locator.locate_all(blocks, page_height=1032, page_width=1920)[0]
    assert len(q.blanks) == 1
    assert q.is_answerable


def test_fill_question_existing_answer_not_in_stem():
    """重跑场景:输入框里已有答案文本(与"第N空"标签同一输入行),
    不得混入题干(LLM 看到旧答案会干扰)也不影响空定位"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1. (填空题)计算20+99=____", 435, 348),
        ("第1空", 435, 428),
        ("119", 490, 429),                        # 已输入的答案
        ("第2空", 435, 623),
        ("110", 490, 624),
    ])
    q = locator.locate_all(blocks, page_height=1032, page_width=1920)[0]
    assert q.stem == "计算20+99=____"
    assert len(q.blanks) == 2
    assert q.is_answerable


def test_fill_question_no_labels_incomplete():
    """填空题但"第N空"标签全部漏检 → 不完整(触发放大重识别),不得误作答"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1. (填空题)计算20+99=____", 435, 348),
    ])
    q = locator.locate_all(blocks, page_height=1032, page_width=1920)[0]
    assert q.qtype == "fill"
    assert not q.blanks
    assert not q.complete
    assert q.incomplete_reason == "未识别到填空输入框"
    assert not q.is_answerable


def test_fill_question_bottom_crop():
    """最后一个输入框贴近视口底部 → 不完整(应滚动,下方可能还有空)"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1. (填空题)计算20+99=____", 435, 348),
        ("第1空", 435, 428),
        ("第2空", 435, 990),                      # region y2=1022 > 1032-60
    ])
    q = locator.locate_all(blocks, page_height=1032, page_width=1920)[0]
    assert not q.complete
    assert q.incomplete_reason == "输入框贴近视口底部"
    assert not q.is_answerable


def test_short_answer_parsed():
    """简答题:题干 + 富文本编辑器。工具栏块("段落格式"等)x1 比题干列
    右移>=25px(CSS padding 特征),须过滤出题干;编辑器点击点=
    (锚点x+260, 题干底+145)。布局取自 2026-09 真实作业页采集。"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("4. (简答题)", 435, 672),
        ("简述Python列表的特点", 435, 715),       # 题干续行(与锚点同列)
        ("段落格式", 460, 745),                    # 工具栏(右移25px)
        ("字体字号", 560, 745),
        ("三三三Q田πbet②", 470, 748),
        ("回)>", 900, 748),
    ])
    questions = locator.locate_all(blocks, page_height=1032, page_width=1920)
    assert len(questions) == 1
    q = questions[0]
    assert q.qtype == "short_answer"
    assert q.number == 4
    assert q.stem == "简述Python列表的特点"
    assert "段落格式" not in q.stem and "字体字号" not in q.stem
    # 编辑器点击点:锚点x+260,题干底(735)+145
    assert q.editor_center == (435 + 260, 735 + 145)
    assert q.complete
    assert q.is_answerable
    # prompt 含人味文风要求(大学生做课后作业口吻,不过于口语化)
    assert "标点" in q.to_prompt_text()
    assert "大学生" in q.to_prompt_text()
    assert "不要过于口语化" in q.to_prompt_text()


def test_short_answer_editor_bottom_crop():
    """编辑器点击点贴近视口底部 → 不完整(应滚动露出编辑器)"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("4. (简答题)简述Python列表的特点", 435, 900),
    ])
    q = locator.locate_all(blocks, page_height=1032, page_width=1920)[0]
    # 题干底=920,点击点y=1065 > 1032-60
    assert not q.complete
    assert q.incomplete_reason == "编辑器贴近视口底部"
    assert not q.is_answerable


def test_fill_short_choice_mixed_page():
    """混合题型页:选择+填空+简答同屏互不污染"""
    locator = QuestionLocator()
    blocks = make_blocks([
        ("1. (单选题)1+1=", 435, 348),
        ("A", 450, 420), ("2", 486, 421),
        ("B", 450, 470), ("3", 486, 470),
        ("2. (填空题)计算20+99=____", 435, 560),
        ("第1空", 435, 640),
        ("3. (简答题)简述Python的特点", 435, 760),
        ("段落格式", 460, 830),
    ])
    questions = locator.locate_all(blocks, page_height=1032, page_width=1920)
    assert len(questions) == 3
    q1, q2, q3 = questions
    assert q1.qtype == "single" and q1.options == {"A": "2", "B": "3"}
    assert q2.qtype == "fill" and len(q2.blanks) == 1
    assert q3.qtype == "short_answer" and q3.editor_center is not None
    assert all(q.is_answerable for q in questions)
