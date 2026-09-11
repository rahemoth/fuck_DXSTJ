# -*- coding: utf-8 -*-
"""
任务执行器:做题主循环(MAA pipeline 思想:截图 → 识别 → 决策 → 执行)。

支持两种页面形态(自动适配):
- 长滚动页(作业作答):所有题在一页,答完可见题目后向下滚动找新题
- 单题翻页页(考试等):答完点'下一题'按钮

运行在工作线程中(GUI 通过信号接收事件),支持随时停止。
"""
import copy
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
from PIL import Image

from core.agent.llm import LLMClient
from core.agent.solver import Solver
from core.controller.input import InputController
from core.controller.window import WindowCapture
from core.log import get_logger
from core.pipeline.scanner import PageScanner
from core.vision.locator import QuestionLocator, Question
from core.vision.ocr import OcrEngine

logger = get_logger("pipeline.executor")

# 连续空滚动次数达到该值认为已到页面底部
_EMPTY_SCROLL_LIMIT = 3
# 滚动总次数安全上限
_SCROLL_CAP = 400
# 每题方向键微滚尝试上限(超过则大步滚动跳过,留待回顶复查再试)
_FINE_TRIES = 8
# 连续无法识别页面次数上限
_UNKNOWN_LIMIT = 60
# 填空题输入框空状态的占位符"第N空"(键入验证时排除)
_BLANK_PLACEHOLDER_RE = re.compile(r"^第\s*\d+\s*空$")


@dataclass
class ExecutorEvent:
    """执行器事件(GUI 展示用)"""
    kind: str        # question / answer / done / error
    data: dict


class StopRequested(Exception):
    pass


class Executor:
    def __init__(self, cfg: dict, emit=None):
        """
        :param cfg: 完整配置字典
        :param emit: 事件回调 emit(ExecutorEvent),GUI 订阅
        """
        self.cfg = cfg
        self.emit = emit or (lambda e: None)
        self._stop = threading.Event()

        self.window = WindowCapture(cfg["window"]["title_keywords"],
                                    cfg["window"]["capture_method"])
        self.ocr = OcrEngine(cfg["ocr"]["confidence_threshold"])
        self.locator = QuestionLocator(cfg["roi"])
        self.input = InputController(self.window, cfg["action"], cfg["roi"])
        self.llm = LLMClient(cfg["llm"])
        self.solver = Solver(self.llm, max_retries=cfg["llm"]["max_retries"])
        self.scanner = None                 # 批量模式整页扫描器(懒建)

        # 统计与状态
        self.done_count = 0
        self.fail_count = 0
        self._done_keys: set[str] = set()   # 已成功题(题干key去重,避免OCR重读差异重复计数)
        self._failed_keys: set[str] = set()  # 失败题(题干key去重)
        self.processed: set[str] = set()   # 已处理题目(题干key;窄窗口题号可能被裁剪/误读,不可靠)
        self._unknown_streak = 0
        self._empty_scrolls = 0            # 连续滚动页面无移动(=到底)
        self._scroll_total = 0
        self._swept = False                # 是否已做过回顶复查
        self._partial_tries: dict[str, int] = {}  # 各题微滚尝试次数(按题干key)

    # ---------- 生命周期 ----------

    def stop(self):
        self._stop.set()
        logger.info("已请求停止")

    def _check_stop(self):
        if self._stop.is_set():
            raise StopRequested()

    # ---------- 主循环 ----------

    def run(self):
        """做题主循环,在工作线程中调用"""
        logger.info("===== 开始执行 =====")
        try:
            self.window.ensure_connected()
            self.input.set_home()   # 记录鼠标起始位置,每个动作后复位到此
            # 做题模式:per_question=逐题识别作答(旧模式);
            # long_screenshot=整页长图扫描批量作答,页面不适合时自动回退逐题
            mode = self.cfg["action"]["answer_mode"]
            if mode == "per_question":
                logger.info("做题模式:逐题识别作答")
                self._loop()
            elif not self._run_batch():
                self._loop()
        except StopRequested:
            logger.info("已停止")
        except Exception as e:
            logger.exception(f"执行异常终止: {e}")
            self.emit(ExecutorEvent("error", {"message": str(e)}))
        finally:
            self.input.restore_home()   # 结束后把鼠标还给用户
            summary = f"共完成 {self.done_count} 题,失败 {self.fail_count} 题"
            logger.info(f"===== 结束:{summary} =====")
            self.emit(ExecutorEvent("done", {"summary": summary}))

    # ---------- 批量模式:整页扫描 → 并发求解 → 定位滚动作答 ----------

    def _run_batch(self) -> bool:
        """整页长图扫描批量模式。滚动条不可用/单题翻页页/扫描异常返回
        False(调用方回退旧的逐题循环)。"""
        self.scanner = PageScanner(self.window, self.ocr, self.locator,
                                  self.input, self.cfg,
                                  check_stop=self._check_stop)
        try:
            scan = self.scanner.scan()
        except Exception as e:
            logger.warning(f"整页扫描异常({e}),回退逐题模式")
            return False
        if scan is None:
            return False
        # 单题翻页页(考试):可见"下一题"按钮,逐题翻页流程更合适
        if self.locator.find_next_button(scan.blocks) is not None:
            logger.info("检测到'下一题'按钮(单题翻页页),回退逐题模式")
            return False

        self._heal_incomplete(scan)
        questions = scan.questions
        if not questions:
            logger.warning("整页扫描未解析出题目,回退逐题模式")
            return False

        # 并发求解(按页面顺序作答,LLM 调用并发取回)
        answers = self._solve_all(questions)

        for q in questions:
            self._check_stop()
            if q.key in self._done_keys:
                continue
            answer = answers.get(q.key)
            num = q.number if q.number is not None else "(题号未识别)"
            if not q.is_answerable:
                logger.info(f"题目{num} 信息不完整({q.incomplete_reason}),跳过")
                self._failed_keys.add(q.key)
                self.fail_count = len(self._failed_keys)
                continue
            if answer is None:
                logger.error(f"题目{num} 获取答案失败,跳过")
                self._failed_keys.add(q.key)
                self.fail_count = len(self._failed_keys)
                continue
            self.emit(ExecutorEvent("question", {
                "qtype": q.qtype, "stem": q.stem, "options": q.options,
            }))
            logger.info(f"题目{num} 答案: {answer}")
            self.emit(ExecutorEvent("answer", {"answer": answer}))
            ok = self._answer_in_scan(q, answer, scan)
            if ok:
                self._done_keys.add(q.key)
                self.done_count = len(self._done_keys)
            else:
                self._failed_keys.add(q.key)
                self.fail_count = len(self._failed_keys)
        logger.info("批量作答完毕。如需提交,请在学习通中手动点击提交按钮")
        return True

    def _heal_incomplete(self, scan):
        """对长图上选项不完整的题目做选项区放大重识别(复用 zoom 机制)"""
        for _round in range(2):
            incomplete = [q for q in scan.questions
                          if q.stem and not q.complete and q.anchor_y2 > 0]
            if not incomplete:
                break
            healed = False
            for q in incomplete:
                zoomed = self._ocr_zoom_band(scan.long_img, scan.blocks, q,
                                              page_height=scan.max_offset + 200)
                if zoomed is None:
                    continue
                new_blocks, new_questions = zoomed
                new_q = next((nq for nq in new_questions if nq.key == q.key), None)
                if new_q is not None and new_q.complete:
                    logger.info(f"题目{q.number} 放大重识别后完整"
                                f"(选项={dict(new_q.options)})")
                    scan.blocks, scan.questions = new_blocks, new_questions
                    healed = True
                    break           # blocks 已整体替换,重取 incomplete 列表
            if not healed:
                break

    def _solve_all(self, questions: list[Question]) -> dict[str, object]:
        """并发调用 LLM 求解全部题目,返回 {题干key: 答案}。
        单题异常记 None(作答阶段按失败计)。"""
        workers = int(self.cfg["llm"]["concurrency"])
        answers: dict[str, object] = {}

        def solve_one(q: Question):
            self._check_stop()
            return self.solver.solve(q)

        if workers <= 1:
            for q in questions:
                try:
                    answers[q.key] = solve_one(q)
                except StopRequested:
                    raise
                except Exception as e:
                    logger.error(f"题目{q.number} 求解失败: {e}")
                    answers[q.key] = None
            return answers

        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = {ex.submit(solve_one, q): q for q in questions}
            for fut in as_completed(futs):
                q = futs[fut]
                try:
                    answers[q.key] = fut.result()
                except StopRequested:
                    raise
                except Exception as e:
                    logger.error(f"题目{q.number} 求解失败: {e}")
                    answers[q.key] = None
        finally:
            # 停止时不等待在途 LLM 调用(各有硬超时兜底,线程自会退出)
            ex.shutdown(wait=False, cancel_futures=True)
        return answers

    def _answer_in_scan(self, q: Question, answer, scan) -> bool:
        """把长图坐标的题目滚进视口并作答。视口容不下的超高题按
        选项/输入框分块滚动作答。"""
        h = scan.viewport_h
        if q.qtype == "fill":
            ys = [b["center"][1] for b in q.blanks]
            if not ys:
                return False
            if max(ys) - min(ys) <= h - 200:
                offset = scan.scroll_to(q.blanks[0]["center"][1] - h // 3)
                if offset < 0:
                    return False
                return self._execute_answer(self._to_viewport(q, offset), answer)
            ok = True
            for i, blank in enumerate(q.blanks):
                if i >= len(answer):
                    break
                offset = scan.scroll_to(blank["center"][1] - h // 2)
                if offset < 0:
                    ok = False
                    continue
                one = copy.deepcopy(q)
                one.blanks = [self._shift_blank(blank, offset)]
                if not self._execute_answer(one, [answer[i]]):
                    ok = False
            return ok
        if q.qtype == "short_answer":
            if q.editor_center is None:
                return False
            offset = scan.scroll_to(q.editor_center[1] - h // 3)
            if offset < 0:
                return False
            return self._execute_answer(self._to_viewport(q, offset), answer)

        # 选择/判断题
        ys = [cy for _l, (_x, cy) in q.option_centers.items()]
        if not ys:
            return False
        if max(ys) - min(ys) <= h - 200:
            offset = scan.scroll_to(min(ys) - h // 3)
            if offset < 0:
                return False
            return self._execute_answer(self._to_viewport(q, offset), answer)
        # 超高题:按选项分块滚动,逐块点击+验证(only 模式不触碰块外
        # 选项,避免把其他块已答的选项取消)
        labels = [l for l in answer if l in q.option_centers]
        if self.input.dry_run:
            self.input.click_options(q.option_centers, labels)
            return True
        ok = True
        for label in labels:
            offset = scan.scroll_to(q.option_centers[label][1] - h // 2)
            if offset < 0:
                ok = False
                continue
            vq = self._to_viewport(q, offset)
            if not self._click_with_verify(vq, [label], only=True):
                ok = False
        return ok

    def _to_viewport(self, q: Question, offset: int) -> Question:
        """长图坐标 → 视口坐标的题目副本(点击/验证都基于当前视口)"""
        vq = copy.deepcopy(q)

        def ty(y: int) -> int:
            return y - offset

        vq.option_centers = {lb: (x, ty(y))
                              for lb, (x, y) in vq.option_centers.items()}
        vq.blanks = [self._shift_blank(b, offset) for b in vq.blanks]
        if vq.editor_center is not None:
            vq.editor_center = (vq.editor_center[0], ty(vq.editor_center[1]))
        vq.anchor_y2 = ty(vq.anchor_y2)
        vq.region_y2 = ty(vq.region_y2)
        return vq

    @staticmethod
    def _shift_blank(blank: dict, offset: int) -> dict:
        x1, y1, x2, y2 = blank["region"]
        cx, cy = blank["center"]
        return {"index": blank["index"], "center": (cx, cy - offset),
                "region": (x1, y1 - offset, x2, y2 - offset)}

    def _loop(self):
        while not self._stop.is_set():
            self._check_stop()
            result = self._step()
            if result == "done":
                logger.info("所有题目处理完毕。如需提交,请在学习通中手动点击提交按钮")
                break
            if result == "retry":
                time.sleep(1.5)

    def _step(self) -> str:
        """执行一步。返回 handled / scrolled / retry / done"""
        img = self.window.screenshot()
        blocks = self.ocr.run(img)
        questions = self.locator.locate_all(blocks, img.size[1], img.size[0])

        target, partial = self._pick_target(questions)
        if target is None and partial is not None:
            # 单字符选项(单个数字/字母圈)体积极小,OCR置信度低易整块漏检
            # (实测Q6选项全为单个数字时0.55阈值下全丢),降阈值对同一截图重识别
            blocks = self.ocr.run(img, threshold=self.cfg["ocr"]["retry_threshold"])
            questions = self.locator.locate_all(blocks, img.size[1], img.size[0])
            target, partial = self._pick_target(questions)

        if (target is None and partial is not None
                and "贴近视口底部" not in (partial.incomplete_reason or "")
                and (not partial.options or partial.incomplete_reason)):
            # 零选项或部分漏检(标签不连续/题干截断/间距过大):
            # RapidOCR 检测阶段就漏掉单字符块(如选项"0"/"1",置信度无关),
            # 裁剪该题区域放大3倍重识别,小目标放大后可检出。
            # "贴近视口底部"除外(含填空"输入框/编辑器贴近视口底部"):
            # 输入区在视口外,放大无用,应滚动。填空题的"第N空"标签漏检时
            # 低阈值放大重识别同样能找回。
            zoomed = self._ocr_zoom_band(img, blocks, partial)
            if zoomed is not None:
                blocks, questions = zoomed
                target, partial = self._pick_target(questions)

        next_btn = self.locator.find_next_button(blocks)

        if target is not None:
            self._unknown_streak = 0
            self._handle_question(target, next_btn)
            return "handled"

        # 题干可见但选项不完整 → 优先微滚精准露出选项
        if partial is not None:
            num = partial.number if partial.number is not None else partial.stem[:15]
            reason = partial.incomplete_reason or "未采集到选项"
            if self._partial_tries.get(partial.key, 0) == 0:
                # 首次发现该题不完整:输出诊断信息,用于定位版式/裁剪问题
                logger.info(f"[诊断] 题目{num} {reason},已识别选项={dict(partial.options)}")
                logger.info("[诊断] 屏幕文本块: " + " | ".join(
                    f"{b.text}@{b.box[0]},{b.box[1]}"
                    for b in blocks if self.locator._in_region(b)))
            return self._scroll_partial(partial, f"题目{num}选项不完整({reason})", img)

        # 单题翻页模式:当前题已处理,点'下一题'
        if questions and next_btn is not None:
            self._click_next(next_btn)
            return "handled"

        if not questions:
            self._unknown_streak += 1
            if self._unknown_streak % 5 == 1:
                logger.info(f"当前页面无法识别题目(第{self._unknown_streak}次),可能是加载中或非答题页")
            if self._unknown_streak > _UNKNOWN_LIMIT:
                raise RuntimeError("连续多次无法识别页面,请人工检查(是否弹窗遮挡/题型不支持)")
            return "retry"

        # 可见题目均已处理 → 滚动查找新题
        return self._do_scroll("当前可见题目均已处理", img)

    def _pick_target(self, questions: list[Question]):
        """从当前题目列表中选取处理对象:
        第一个未处理且可作答的题目;同时记录题干可见但选项不完整的题"""
        target = None
        partial = None
        for q in questions:
            if q.key in self.processed:
                continue
            if q.is_answerable:
                target = q
                break
            if partial is None and q.stem:
                partial = q
        return target, partial

    def _ocr_zoom_band(self, img, blocks, partial: Question, page_height=None):
        """裁剪题目选项区(anchor_y2 ~ region_y2)放大3倍重识别。
        单字符选项(如"3"/"5"/字母圈)在整页OCR的检测阶段就漏检,
        放大后可检出。识别块坐标映射回客户区后与整页块合并重新解析。
        返回 (新blocks, 新questions) 或 None(不适用的情形)。
        :param page_height: 覆盖"贴近视口底部"检查用的高度
            (整页长图扫描时传 长图高+余量:整页无截断)"""
        y1, y2 = partial.anchor_y2, partial.region_y2
        y2 = min(y2, y1 + 420)                 # 选项区不会超过一屏
        if y1 <= 0 or y2 - y1 < 40:
            return None
        rx1, _, _, _ = self.locator.region
        # 右界动态收缩,把窄窗口下左移进入视野的答题卡裁在带外
        x1, x2 = max(0, rx1), min(img.size[0], self.locator.content_x2(img.size[0]))
        scale = 3
        band = img.crop((x1, y1, x2, y2))
        band = band.resize((band.size[0] * scale, band.size[1] * scale),
                           Image.LANCZOS)
        band_blocks = self.ocr.run(band, threshold=self.cfg["ocr"]["retry_threshold"])
        if not band_blocks:
            return None
        from core.vision.ocr import OcrBlock
        mapped = []
        for b in band_blocks:
            bx1, by1, bx2, by2 = b.box
            mapped.append(OcrBlock(
                text=b.text,
                box=(x1 + bx1 // scale, y1 + by1 // scale,
                     x1 + bx2 // scale, y1 + by2 // scale),
                confidence=b.confidence))
        logger.info(f"[诊断] 题目{partial.number} 选项区({y1}~{y2}px)放大重识别:"
                    f"新增 {len(mapped)} 块 {[b.text for b in mapped]}")
        # 用映射回的块替换该区域的旧块(旧块基本为空),其余区域保留。
        # 必须按 y 重排:locate_all 按列表索引切分题目区域,依赖块有序;
        # mapped 追加在末尾而 y 在页面中部,会把放大块划给最后一题
        # (实测Q6选项块被划给Q7,Q6仍空、Q7带着错误选项去作答)
        merged = [b for b in blocks if not (y1 <= (b.box[1] + b.box[3]) / 2 < y2)] + mapped
        merged.sort(key=lambda b: (b.box[1], b.box[0]))
        questions = self.locator.locate_all(merged, page_height or img.size[1], img.size[0])
        return merged, questions

    # ---------- 内部 ----------

    def _handle_question(self, q, next_btn):
        num = q.number if q.number is not None else "(题号未识别)"
        logger.info(f"识别到题目{num}[{q.qtype}]: {q.stem[:50]}...")
        self.emit(ExecutorEvent("question", {
            "qtype": q.qtype, "stem": q.stem, "options": q.options,
        }))

        # 获取答案
        try:
            answer = self.solver.solve(q)
        except StopRequested:
            raise
        except Exception as e:
            self._failed_keys.add(q.key)
            self.fail_count = len(self._failed_keys)
            self.processed.add(q.key)   # 跳过也算处理过,避免死循环
            logger.error(f"题目{num}获取答案失败,跳过: {e}")
            if next_btn is not None:
                self._click_next(next_btn)
            return

        logger.info(f"题目{num} 答案: {answer}")
        self.emit(ExecutorEvent("answer", {"answer": answer}))

        ok = self._execute_answer(q, answer)
        if ok:
            self._done_keys.add(q.key)
            self.done_count = len(self._done_keys)
        else:
            self._failed_keys.add(q.key)
            self.fail_count = len(self._failed_keys)
        self.processed.add(q.key)
        self._empty_scrolls = 0

        if next_btn is not None:
            self._click_next(next_btn)

    def _execute_answer(self, q: Question, answer) -> bool:
        """按题型分发执行答案动作(dry-run 下输入方法自身跳过,验证放行)"""
        self._check_stop()
        if q.qtype == "fill":
            return self._fill_blanks(q, answer)
        if q.qtype == "short_answer":
            return self._type_answer(q, answer)
        if self.input.dry_run:
            self.input.click_options(q.option_centers, answer)
            return True
        return self._click_with_verify(q, answer)

    def _fill_blanks(self, q: Question, answers: list[str]) -> bool:
        """填空题:逐空 点击输入框 → Ctrl+A 全选 → 逐字符键入 → 区域 OCR 验证。
        学习通 JS 禁用粘贴,只能键盘键入(见 InputController.type_text);
        Ctrl+A 后键入为覆盖式,重试/重跑幂等(不会追加出双份答案)。
        验证不用暗像素计数:空输入框基线即有 ~108 个暗像素(边框/占位符),
        短答案增量不足以区分;OCR 直接核对内容更可靠。"""
        num = q.number if q.number is not None else q.key[:12]
        ok_all = True
        for i, blank in enumerate(q.blanks):
            if i >= len(answers):
                break
            cx, cy = blank["center"]
            text = answers[i]
            for attempt in range(2):
                self.input.click_client(cx, cy, label=f"题目{num} 第{blank['index']}空")
                time.sleep(self.cfg["action"]["verify_wait"])
                self.input.select_all()
                self.input.type_text(text)
                time.sleep(self.cfg["action"]["verify_wait"])
                if self.input.dry_run:
                    break
                got = self._ocr_region(blank["region"])
                if got and not _BLANK_PLACEHOLDER_RE.match(got):
                    logger.info(f"题目{num} 第{blank['index']}空 已填写(OCR: {got!r})")
                    break
                if attempt == 0:
                    logger.warning(
                        f"题目{num} 第{blank['index']}空 未检出输入内容(OCR: {got!r}),重试")
            else:
                logger.warning(f"题目{num} 第{blank['index']}空 重试后仍未检出,请人工检查")
                ok_all = False
        return ok_all

    def _type_answer(self, q: Question, answers: list[str]) -> bool:
        """简答题:点击富文本编辑器 → Ctrl+A 全选 → 逐字符键入 → 区域 OCR 验证。
        空编辑器点击后光标落在内容区顶部,键入文本从内容区顶部排布;
        验证区覆盖点击点上下两侧。人味答案均在百字以上,以 OCR 出的
        文本长度 >= 15 字为已作答判据(空编辑器该区域无文本)。"""
        num = q.number if q.number is not None else q.key[:12]
        text = answers[0] if answers else ""
        cx, cy = q.editor_center
        region = (cx - 150, cy - 65, cx + 350, cy + 80)
        for attempt in range(2):
            self.input.click_client(cx, cy, label=f"题目{num} 简答编辑器")
            time.sleep(self.cfg["action"]["verify_wait"])
            self.input.select_all()
            self.input.type_text(text)
            time.sleep(self.cfg["action"]["verify_wait"])
            if self.input.dry_run:
                return True
            got = self._ocr_region(region)
            if len(got) >= 15:
                logger.info(f"题目{num} 简答题已作答(OCR前30字: {got[:30]!r})")
                return True
            if attempt == 0:
                logger.warning(f"题目{num} 简答题未检出输入内容(OCR: {got!r}),重试")
        logger.warning(f"题目{num} 简答题重试后仍未检出,请人工检查")
        return False

    def _ocr_region(self, region, scale: int = 2) -> str:
        """裁剪区域放大后 OCR,返回识别文本(键入验证用)。
        输入框内文字较小,1x 识别率低,放大 2 倍提升召回;
        右界收缩到内容区右界,排除窄窗口下左侧移入的答题卡文字。"""
        img = self.window.screenshot()
        x1, y1, x2, y2 = region
        x2 = min(x2, self.locator.content_x2(img.size[0]))
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        if x2 <= x1 or y2 <= y1:
            return ""
        crop = img.crop((x1, y1, x2, y2))
        if scale != 1:
            crop = crop.resize((crop.size[0] * scale, crop.size[1] * scale),
                               Image.LANCZOS)
        blocks = self.ocr.run(crop,
                             threshold=self.cfg["ocr"]["retry_threshold"])
        blocks = sorted(blocks, key=lambda b: (b.box[1], b.box[0]))
        return "".join(b.text.strip() for b in blocks).strip()

    def _click_with_verify(self, q: Question, labels: list[str],
                           only: bool = False) -> bool:
        """点击选项并用选中状态检测验证(选项前圆圈选中后变蓝色)。
        注意:学习通选项为切换式,重复点击会反选,因此补点仅限
        "检出目标未选中"的情形,避免对已选中目标重复点击造成反选;
        点击前先检测,已选中的目标选项跳过,已选中的非目标选项点击取消(纠正遗留)。
        :param only: 超高题分块作答时只处理 labels 内的选项,
            不对块外选项做取消纠正(后续块/已答选项不在本块)"""
        centers = q.option_centers
        num = q.number if q.number is not None else q.key[:12]
        pending = [l for l in labels if l in centers]
        extras = [l for l in centers if l not in labels]

        img = self.window.screenshot()
        ref_img = img          # 点击前帧:复查时帧对齐的参考(点击后页面若自动滚动,与此帧比)
        already = [l for l in pending if self._is_selected(img, *centers[l])]
        if already:
            logger.info(f"题目{num} 选项 {already} 已处于选中状态,跳过点击")
            pending = [l for l in pending if l not in already]
        to_deselect = [] if only else \
            [l for l in extras if self._is_selected(img, *centers[l])]
        if to_deselect:
            logger.info(f"题目{num} 非答案选项 {to_deselect} 已被选中,点击取消")
            self.input.click_options(centers, to_deselect)
            time.sleep(self.cfg["action"]["verify_wait"])

        if pending:
            self.input.click_options(centers, pending)
            time.sleep(self.cfg["action"]["verify_wait"])
        self.input.move_away()
        time.sleep(0.3)

        # 复查并纠正:点击可能引起页面自动滚动(尤其点近视口底部的选项),
        # 每轮以帧间图像对齐(题干锚点位移的图像版)计算滚动偏移,把选项
        # 按原布局整体平移后检测选中状态(见 _fresh_centers 注释:重新
        # 解析的选项坐标不可靠)。以点击前(选中检测用)的截图为参考帧,
        # 各轮复查只需对齐帧位移,免旧实现的整页 OCR 重解析。
        # 首轮发现误选或漏选时主动纠正:先取消误选的非目标选项,再补点
        # 仍未选中的目标;纠正后再次复查确认。题目滚出视口时绝不补点——
        # 旧坐标会点到别的选项,把已选对的答案改成错的。
        for attempt in range(3):
            img = self.window.screenshot()
            centers = self._fresh_centers(q, img, ref_img=ref_img)
            if centers is None:
                logger.info(f"题目{num} 复查时题目已滚出视口,视为已作答(原点击应已生效)")
                return True
            unselected = [l for l in labels
                          if l in centers and not self._is_selected(img, *centers[l])]
            if not unselected:
                return True
            if attempt == 0:
                wrong = [l for l in centers
                         if l not in labels and self._is_selected(img, *centers[l])]
                if not only and wrong:
                    logger.info(f"题目{num} 检出误选选项 {wrong},点击取消")
                    self.input.click_options(centers, wrong)
                    time.sleep(self.cfg["action"]["verify_wait"])
                logger.info(f"题目{num} 选项 {unselected} 未检出选中,补点")
                self.input.click_options(centers, unselected)
                time.sleep(self.cfg["action"]["verify_wait"])
            else:
                time.sleep(0.8)   # 等待选中状态渲染后再复查
        logger.warning(f"题目{num} 选项 {unselected} 点击后未检出选中,请人工检查")
        return False

    def _fresh_centers(self, q: Question, img,
                        ref_img=None) -> dict[str, tuple[int, int]] | None:
        """重新定位题目,返回平移后的最新选项坐标;定位失败或滚出视口返回 None。
        偏移量即点击前后视口的滚动位移,选项按原布局整体平移——
        不复用重新解析出的选项坐标:小字符选项(单个数字/字母圈)
        在整页 OCR 下大量漏检,选项-标签对应关系不可靠,实测把 A 错位到
        下两行,补点会把已选对的答案改成错的。

        批量模式优先用 scanner 的帧间条带对齐(frame_offset)测位移,
        免去旧实现的整页 OCR(每次约 1.4s × 每题最多 3 轮复查):点击只
        改变选项圆圈局部像素(31×31),条带多数表决天然抗局部污染。
        无 scanner/对齐失败时回退整页 OCR 重新解析(原路径,兼容逐题模式
        与内容变化场景)。"""
        # ---- 路径 1:帧间条带对齐(免 OCR) ----
        if ref_img is not None and self.scanner is not None \
                and self.scanner.result is not None:
            moved = self.scanner.frame_offset(ref_img, img)
            if moved is not None:
                # 页面向下滚 moved → 内容上移 moved → 选项 y - moved
                delta = -moved
                h = img.size[1]
                # 边界余量 30px:选项圆圈高约 31px,部分露出视口时选中检测
                # 不可靠(假阴性会触发补点反选已选对的选项),宁可视为已作答
                if any(not (30 < y + delta < h - 30)
                       for (_x, y) in q.option_centers.values()):
                    return None   # 题目(部分)滚出视口,坐标不可用
                return {label: (x, y + delta)
                        for label, (x, y) in q.option_centers.items()}
            logger.debug("复查帧对齐失败,回退整页 OCR 重新定位")
        # ---- 路径 2:整页 OCR 重新解析(兑底) ----
        try:
            blocks = self.ocr.run(img)
            for qq in self.locator.locate_all(blocks, img.size[1], img.size[0]):
                if qq.key != q.key:
                    continue
                delta = qq.anchor_y2 - q.anchor_y2
                h = img.size[1]
                # 边界余量 30px:选项圆圈高约 31px,部分露出视口时选中检测
                # 不可靠(假阴性会触发补点反选已选对的选项),宁可视为已作答
                if any(not (30 < y + delta < h - 30)
                       for (_x, y) in q.option_centers.values()):
                    return None   # 题目(部分)滚出视口,坐标不可用
                return {label: (x, y + delta)
                        for label, (x, y) in q.option_centers.items()}
        except Exception as e:
            logger.debug(f"复查时重新定位题目失败: {e}")
        return None

    def _is_selected(self, img, cx: int, cy: int) -> bool:
        """检测选项是否为选中态。
        学习通选项样式:文字本身为蓝灰色(未选也有~150蓝px),不可用总量判断;
        选中时选项前出现实心蓝色圆圈(约31x31),列投影上单列蓝色像素达25+,
        而文字笔画单列最多约10。以单列最大蓝色像素数>20 为选中判据。
        圆圈位于选项文字左侧约100px,搜索 (cx-170, cy±20)..(cx+10)。"""
        arr = np.asarray(img.convert("RGB"), dtype=np.int16)
        h, w = arr.shape[:2]
        y1, y2 = max(0, cy - 20), min(h, cy + 20)
        x1, x2 = max(0, cx - 170), min(w, cx + 10)
        if y2 <= y1 or x2 <= x1:
            return False
        region = arr[y1:y2, x1:x2]
        r, g, b = region[:, :, 0], region[:, :, 1], region[:, :, 2]
        blue = ((b > 120) & (b - r > 50) & (b - g > 25))
        col_max = int(blue.sum(axis=0).max()) if blue.size else 0
        return col_max > 20

    def _click_next(self, nb):
        self._check_stop()
        self.input.click_client(nb.center[0], nb.center[1], label="下一题")
        self.input.wait_next_page()

    def _scroll_partial(self, partial: Question, reason: str, img) -> str:
        """选项不完整时的滚动策略:
        优先方向键↓小步微滚(精准露出被裁剪的选项),
        每题最多 _FINE_TRIES 次;仍不完整则导航滚动跳过,留待回顶复查再试"""
        tries = self._partial_tries.get(partial.key, 0)
        if tries < _FINE_TRIES:
            self._partial_tries[partial.key] = tries + 1
            return self._do_fine_scroll(reason, img)
        return self._do_scroll(f"{reason},微滚多次无效改用导航滚动", img)

    def _do_fine_scroll(self, reason: str, img_before) -> str:
        """方向键↓小步微滚(约150px,仅露出下一两行选项)。
        不做移动检测:像素对比存在误判(实测页面已移动却判为未动),
        误判后立即升级大步滚动会连跳数题;交由主循环重新截图判断,
        若页面确实未动,同一题会再次触发微滚(有次数上限兜底)。"""
        if self._scroll_total >= _SCROLL_CAP:
            raise RuntimeError(f"滚动超过 {_SCROLL_CAP} 次仍未完成,请人工检查")
        logger.info(f"{reason},↓微滚露出选项")
        self.input.arrow_down(int(self.cfg["action"]["fine_scroll_steps"]))
        self._scroll_total += 1
        time.sleep(self.cfg["action"]["fine_scroll_wait"])
        return "scrolled"

    def _do_scroll(self, reason: str, img_before=None) -> str:
        """向下滚动(方向键↓小步,与微滚同一步幅)。
        不用大步:步幅超过约190px时 _page_moved 的条带对齐搜索范围
        (受最高条带 y0≈0.25h 限制)检测不到,必然误判"未生效",
        触发重试+兜底连滚上千px,一次性跳过多题(实测Q8-10被跳过);
        小步虽需多滚几次,但相邻视图重叠大、移动检测可靠,不跳题。
        方向键未生效(焦点丢失)时重试一次;连续多次滚动页面纹丝不动
        (包括重试) = 已到页面底部。"""
        if self._scroll_total >= _SCROLL_CAP:
            raise RuntimeError(f"滚动超过 {_SCROLL_CAP} 次仍未完成,请人工检查")
        logger.info(reason)
        steps = int(self.cfg["action"]["fine_scroll_steps"])

        # 重试前重新截图做基线(首次可能实际已滚动而检测误判)
        if self._nav_arrows(steps, img_before):
            self._empty_scrolls = 0
            return "scrolled"
        logger.info("↓未生效,重试一次")
        img_before = self.window.screenshot()
        if self._nav_arrows(steps, img_before):
            self._empty_scrolls = 0
            return "scrolled"

        # 两次都未移动:计入连续空滚动(到底判定)
        self._empty_scrolls += 1
        if self._empty_scrolls >= _EMPTY_SCROLL_LIMIT:
            return self._on_bottom()
        return "scrolled"

    def _nav_arrows(self, steps: int, img_before) -> bool:
        """方向键↓滚动并检测页面是否移动"""
        self.input.arrow_down(steps)
        self._scroll_total += 1
        time.sleep(self.cfg["action"]["page_wait"])
        if img_before is None:
            return True
        img_after = self.window.screenshot()
        return self._page_moved(img_before, img_after)

    def _on_bottom(self) -> str:
        """翻页到底后的处理:首次回顶部复查漏答题,二次才真正结束"""
        if not self._swept and self.processed:
            self._swept = True
            logger.info("已翻页到底,回顶部复查是否有漏答题目")
            self.input.press_home()
            time.sleep(self.cfg["action"]["page_wait"])
            self._empty_scrolls = 0
            self._partial_tries.clear()   # 让被跳过的题重新获得微滚机会
            return "scrolled"
        return "done"

    @staticmethod
    def _page_moved(img1, img2) -> bool:
        """检测页面是否滚动:在 img2 中搜索与 img1 内容条带最佳对齐的垂直偏移。
        页面以白底稀疏文字为主,滚动后像素平均差仅2-3,不可用均值判断;
        改为:找到非零对齐偏移(=滚动了)或零偏移但残差大(=内容整体变化)。"""
        a1 = np.asarray(img1.convert("L"), dtype=np.int16)
        a2 = np.asarray(img2.convert("L"), dtype=np.int16)
        if a1.shape != a2.shape:
            return True
        h, w = a1.shape
        x1, x2 = int(w * 0.12), int(w * 0.85)
        # 3个内容条带综合评估,避免单一条带落在空白区
        strip_h = 80
        strips = []
        for y0 in (int(h * 0.25), int(h * 0.5), int(h * 0.75)):
            strips.append((y0, a1[y0:y0 + strip_h, x1:x2]))

        def total_d(off: int) -> float | None:
            s = 0.0
            for y0, strip in strips:
                ya, yb = y0 + off, y0 + off + strip_h
                if ya < 0 or yb > h:
                    return None
                s += float(np.abs(strip - a2[ya:yb, x1:x2]).mean())
            return s

        best_d, best_off = total_d(0), 0
        if best_d is None:
            return True
        for off in range(-500, 501, 5):
            d = total_d(off)
            if d is not None and d < best_d - 1e-6:
                best_d, best_off = d, off
        if abs(best_off) > 6:
            return True        # 非零偏移处内容对齐 = 页面滚动了
        return best_d > 8.0    # 零偏移但残差大 = 内容整体变化(非滚动)
