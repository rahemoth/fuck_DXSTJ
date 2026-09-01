# -*- coding: utf-8 -*-
"""
任务执行器:做题主循环(MAA pipeline 思想:截图 → 识别 → 决策 → 执行)。

支持两种页面形态(自动适配):
- 长滚动页(作业作答):所有题在一页,答完可见题目后向下滚动找新题
- 单题翻页页(考试等):答完点'下一题'按钮

运行在工作线程中(GUI 通过信号接收事件),支持随时停止。
"""
import threading
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

from core.agent.llm import LLMClient
from core.agent.solver import Solver
from core.controller.input import InputController
from core.controller.window import WindowCapture
from core.log import get_logger
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
        self.locator = QuestionLocator()
        self.input = InputController(self.window, cfg["action"])
        self.llm = LLMClient(cfg["llm"])
        self.solver = Solver(self.llm, max_retries=cfg["llm"].get("max_retries", 1))

        # 统计与状态
        self.done_count = 0
        self.fail_count = 0
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
            self._loop()
        except StopRequested:
            logger.info("已停止")
        except Exception as e:
            logger.exception(f"执行异常终止: {e}")
            self.emit(ExecutorEvent("error", {"message": str(e)}))
        finally:
            summary = f"共完成 {self.done_count} 题,失败 {self.fail_count} 题"
            logger.info(f"===== 结束:{summary} =====")
            self.emit(ExecutorEvent("done", {"summary": summary}))

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
        questions = self.locator.locate_all(blocks, img.size[1])

        target, partial = self._pick_target(questions)
        if target is None and partial is not None:
            # 单字符选项(单个数字/字母圈)体积极小,OCR置信度低易整块漏检
            # (实测Q6选项全为单个数字时0.55阈值下全丢),降阈值对同一截图重识别
            blocks = self.ocr.run(img, threshold=self.cfg["ocr"].get("retry_threshold", 0.3))
            questions = self.locator.locate_all(blocks, img.size[1])
            target, partial = self._pick_target(questions)

        if target is None and partial is not None and not partial.options:
            # 降阈值仍零选项:RapidOCR 检测阶段就漏掉单字符块(置信度无关),
            # 裁剪该题选项区放大3倍重识别,小目标放大后可检出
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

    def _ocr_zoom_band(self, img, blocks, partial: Question):
        """裁剪题目选项区(anchor_y2 ~ region_y2)放大3倍重识别。
        单字符选项(如"3"/"5"/字母圈)在整页OCR的检测阶段就漏检,
        放大后可检出。识别块坐标映射回客户区后与整页块合并重新解析。
        返回 (新blocks, 新questions) 或 None(不适用的情形)。"""
        y1, y2 = partial.anchor_y2, partial.region_y2
        y2 = min(y2, y1 + 420)                 # 选项区不会超过一屏
        if y1 <= 0 or y2 - y1 < 40:
            return None
        rx1, _, rx2, _ = self.locator.region
        x1, x2 = max(0, rx1), min(img.size[0], rx2)
        scale = 3
        band = img.crop((x1, y1, x2, y2))
        band = band.resize((band.size[0] * scale, band.size[1] * scale),
                           Image.LANCZOS)
        band_blocks = self.ocr.run(band, threshold=self.cfg["ocr"].get("retry_threshold", 0.3))
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
        questions = self.locator.locate_all(merged, img.size[1])
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
            self.fail_count += 1
            self.processed.add(q.key)   # 跳过也算处理过,避免死循环
            logger.error(f"题目{num}获取答案失败,跳过: {e}")
            if next_btn is not None:
                self._click_next(next_btn)
            return

        logger.info(f"题目{num} 答案: {answer}")
        self.emit(ExecutorEvent("answer", {"answer": answer}))

        # 执行点击(截图对比验证选中状态)
        self._check_stop()
        if self.input.dry_run:
            self.input.click_options(q.option_centers, answer)
            self.done_count += 1
        elif self._click_with_verify(q, answer):
            self.done_count += 1
        else:
            self.fail_count += 1
        self.processed.add(q.key)
        self._empty_scrolls = 0

        if next_btn is not None:
            self._click_next(next_btn)

    def _click_with_verify(self, q: Question, labels: list[str]) -> bool:
        """点击选项并用选中状态检测验证(选项前圆圈选中后变蓝色)。
        注意:学习通选项为切换式,重复点击会反选,因此永不补点;
        点击前先检测,已选中的目标选项跳过,已选中的非目标选项点击取消(纠正遗留)。"""
        centers = q.option_centers
        num = q.number if q.number is not None else q.key[:12]
        pending = [l for l in labels if l in centers]
        extras = [l for l in centers if l not in labels]

        img = self.window.screenshot()
        already = [l for l in pending if self._is_selected(img, *centers[l])]
        if already:
            logger.info(f"题目{num} 选项 {already} 已处于选中状态,跳过点击")
            pending = [l for l in pending if l not in already]
        to_deselect = [l for l in extras if self._is_selected(img, *centers[l])]
        if to_deselect:
            logger.info(f"题目{num} 非答案选项 {to_deselect} 已被选中,点击取消")
            self.input.click_options(centers, to_deselect)
            time.sleep(self.cfg["action"].get("verify_wait", 0.6))

        if pending:
            self.input.click_options(centers, pending)
            time.sleep(self.cfg["action"].get("verify_wait", 0.6))
        self.input.move_away()
        time.sleep(0.3)

        # 复查:点击可能引起页面自动滚动(尤其点近视口底部的选项),
        # 先重新定位该题拿最新坐标,再检测选中状态;未选中只告警(不补点,防反选)
        for attempt in range(2):
            unselected = self._verify_unselected(q, labels)
            if not unselected:
                return True
            time.sleep(0.8)
        logger.warning(f"题目{num} 选项 {unselected} 点击后未检出选中(已禁止补点防反选),请人工检查")
        return False

    def _verify_unselected(self, q: Question, labels: list[str]) -> list[str]:
        """重新截图定位题目(坐标可能因页面自动滚动而偏移),返回未选中的选项"""
        img = self.window.screenshot()
        centers = dict(q.option_centers)
        try:
            blocks = self.ocr.run(img)
            for qq in self.locator.locate_all(blocks, img.size[1]):
                if qq.key == q.key and qq.option_centers:
                    centers.update(qq.option_centers)
                    break
        except Exception as e:
            logger.debug(f"复查时重新定位题目失败,沿用原坐标: {e}")
        return [l for l in labels
                if l in centers and not self._is_selected(img, *centers[l])]

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
        self.input.arrow_down(int(self.cfg["action"].get("fine_scroll_steps", 3)))
        self._scroll_total += 1
        time.sleep(self.cfg["action"].get("fine_scroll_wait", 0.6))
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
        steps = int(self.cfg["action"].get("fine_scroll_steps", 3))

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
        time.sleep(self.cfg["action"].get("page_wait", 1.0))
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
            time.sleep(self.cfg["action"].get("page_wait", 1.0))
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
