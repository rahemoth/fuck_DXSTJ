# -*- coding: utf-8 -*-
"""
输入控制器:模拟鼠标点击/输入。

采用 pyautogui(SendInput 真实点击),对 CEF/Chromium 窗口可靠;
所有点击带随机延时,防检测。
"""
import ctypes
import random
import time
from contextlib import contextmanager

import pyautogui

from core.config import DEFAULT_CONFIG
from core.controller.window import WindowCapture
from core.log import get_logger

logger = get_logger("controller.input")

# 防止 pyautogui 触发 FailSafeException 中断(鼠标移到角落时停止)
pyautogui.FAILSAFE = False

# ---- SendInput KEYEVENTF_UNICODE:逐字符键入 Unicode 文本 ----
_INPUT_KEYBOARD = 1
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP = 0x0002


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort))


class _INPUT_UNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT),
                ("hi", _HARDWAREINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", ctypes.c_ulong), ("ii", _INPUT_UNION))


def _send_unicode_key(ch: str):
    """发送一个 Unicode 字符的 按下+抬起 事件(与输入法提交字符的事件一致)"""
    code = ord(ch)
    for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
        inp = _INPUT()
        inp.type = _INPUT_KEYBOARD
        inp.ii.ki = _KEYBDINPUT(0, code, flags, 0, None)
        n = ctypes.windll.user32.SendInput(1, ctypes.byref(inp),
                                           ctypes.sizeof(inp))
        if n != 1:
            logger.warning(f"SendInput 失败(返回 {n}, 字符 U+{code:04X})")


class InputController:
    def __init__(self, window: WindowCapture, action_cfg: dict,
                 roi_cfg: dict | None = None):
        self.window = window
        self.cfg = action_cfg
        self.dry_run = action_cfg["dry_run"]
        self._home: tuple[int, int] | None = None   # 鼠标复位点(执行开始时的位置)
        self._in_action = False                     # 嵌套动作标记(仅最外层复位)
        # 布局标定(config.yaml 的 roi 节,执行器注入;零参回退内置 schema)
        self._roi_cfg = roi_cfg if roi_cfg is not None else DEFAULT_CONFIG["roi"]
        self._safe_column: int | None = None        # 焦点安全列缓存(客户区 x)
        self._safe_column_w: int | None = None      # 探测时的客户区宽(变化需重探)

    # ---------- 鼠标复位 ----------

    def set_home(self):
        """记录当前鼠标位置,作为每个动作结束后的复位点(执行开始时调用一次)"""
        try:
            self._home = pyautogui.position()
        except Exception:
            self._home = None

    def restore_home(self):
        """立即把鼠标复位到 set_home 记录的位置"""
        if self.dry_run or self._home is None:
            return
        try:
            pyautogui.moveTo(self._home)
        except Exception as e:
            logger.debug(f"鼠标复位失败(可忽略): {e}")

    @contextmanager
    def _cursor_guard(self):
        """动作期间借用鼠标,结束后复位(嵌套调用仅最外层复位)。
        复位优先回到 set_home() 记录的起始位置;未记录则回到本次动作前的位置;
        dry-run 不移动鼠标,直接透传。"""
        if self.dry_run or self._in_action:
            yield
            return
        self._in_action = True
        try:
            target = self._home
            if target is None:
                try:
                    target = pyautogui.position()
                except Exception:
                    target = None
            try:
                yield
            finally:
                if target is not None:
                    try:
                        pyautogui.moveTo(target)
                    except Exception as e:
                        logger.debug(f"鼠标复位失败(可忽略): {e}")
        finally:
            self._in_action = False

    @staticmethod
    def _sleep(range_s: list | tuple):
        """在 [min, max] 范围内随机延时"""
        lo, hi = range_s if len(range_s) == 2 else (range_s[0], range_s[0])
        time.sleep(random.uniform(float(lo), float(hi)))

    def _click_screen(self, x: int, y: int, label: str = ""):
        """点击屏幕绝对坐标"""
        if self.dry_run:
            logger.info(f"[dry-run] 跳过点击 {label} @屏幕({x},{y})")
            return
        self.window.bring_to_front()
        time.sleep(0.2)
        pyautogui.click(x, y)
        logger.info(f"已点击 {label} @屏幕({x},{y})")

    def click_client(self, x: int, y: int, label: str = "", delay: bool = True):
        """点击客户区坐标(x, y 为截图坐标系)"""
        if delay:
            self._sleep(self.cfg["click_delay"])
        with self._cursor_guard():
            sx, sy = self.window.client_to_screen(int(x), int(y))
            self._click_screen(sx, sy, label)

    def click_options(self, option_centers: dict[str, tuple[int, int]], labels: list[str]):
        """依次点击多个选项(多选题)。labels 形如 ["A", "C"]"""
        with self._cursor_guard():
            for i, label in enumerate(labels):
                if label not in option_centers:
                    logger.warning(f"选项 {label} 无坐标,跳过")
                    continue
                if i > 0:
                    self._sleep(self.cfg["option_interval"])
                x, y = option_centers[label]
                self.click_client(x, y, label=f"选项{label}", delay=False)

    def move_away(self):
        """把鼠标移到客户区左上角,避免悬停高亮干扰点击后的截图验证。
        复位由下一个动作的 _cursor_guard 完成(截图前需保持移开状态)。"""
        if self.dry_run:
            return
        try:
            sx, sy = self.window.client_to_screen(5, 5)
            pyautogui.moveTo(sx, sy)
        except Exception as e:
            logger.debug(f"移开鼠标失败(可忽略): {e}")

    def wait_next_page(self):
        """点击'下一题'后的等待"""
        self._sleep(self.cfg["next_delay"])

    def _focus_content(self):
        """点击内容区空白列建立键盘焦点(方向键/Home 作用于此前的
        焦点元素,需先点空白处把焦点落到页面上)。
        空白列不硬编码:动态探测深蓝侧边栏右缘,点击其右侧的页面左内边距
        (不同分辨率/DPI/窗口宽度下侧边栏物理宽度不同,固定 x=70 在部分
        机器上会落到侧边栏图标上,误点跳转页面、方向键滚动失效)。"""
        self.window.bring_to_front()
        time.sleep(0.1)
        col = self._safe_click_column()
        l, t, r, b = self.window.client_rect_screen()
        sx, sy = self.window.client_to_screen(col, (b - t) // 2)
        pyautogui.click(sx, sy)
        time.sleep(0.15)

    def _safe_click_column(self) -> int:
        """返回建立焦点用的安全空白列(客户区 x)。
        优先动态探测,失败回退配置 roi.focus_click_x(默认 70)。
        结果按客户区宽度缓存:宽度不变时整个运行期复用(侧边栏宽度
        会话内不变),窗口被拉宽/缩窄时重新探测。"""
        try:
            l, t, r, b = self.window.client_rect_screen()
            width = r - l
        except Exception:
            return int(self._roi_cfg["focus_click_x"])
        if self._safe_column is not None and self._safe_column_w == width:
            return self._safe_column
        col = self._detect_safe_column()
        if col is None:
            col = int(self._roi_cfg["focus_click_x"])
            logger.info(f"侧边栏动态探测未生效,焦点列回退配置 x={col}")
        self._safe_column = col
        self._safe_column_w = width
        return col

    def _detect_safe_column(self) -> int | None:
        """动态探测安全空白列:深蓝侧边栏右缘右侧的页面左内边距。
        1) 侧边栏右缘:从 x=0 连续延伸的深蓝竖带(列内多数像素深蓝,
           允许 ≤3px 边框/抗锯齿噪声),右缘=最后一个深蓝列+1
        2) 内容首列:右缘后第一个"持续有墨"的列(整列暗像素≥8且连续
           3列,过滤单列渲染噪声),即题干/字母圈等内容的左界
        3) 空白列=右缘+偏移:紧贴侧边栏的页面左内边距在所有版式下均
           为空白(压缩窗 48px/全屏更宽),偏移不超过与内容首列的
           中点、上限 20px,避免误触选项字母圈(radio 误点即改答案)
        探测失败(无深蓝侧边栏/截图异常/结果越界)返回 None。"""
        try:
            import numpy as np

            img = self.window.screenshot()
            arr = np.asarray(img.convert("RGB"), dtype=np.int16)
            h, w = arr.shape[:2]
            if w < 60 or h < 120:
                return None
            # 垂直条带去掉顶底各5%(避开窗口边缘/边框线渲染差异)
            band = arr[int(h * 0.05):int(h * 0.95)]
            # --- 1) 侧边栏右缘 ---
            scan_w = min(w, int(w * 0.4))
            sub = band[:, :scan_w]
            blue = ((sub[:, :, 2] > 60) & (sub[:, :, 2] - sub[:, :, 0] > 20)
                    & (sub[:, :, 2] - sub[:, :, 1] > 8))
            col_blue = blue.mean(axis=0)
            edge, miss = 0, 0
            for x in range(scan_w):
                if col_blue[x] > 0.55:
                    edge, miss = x + 1, 0
                else:
                    miss += 1
                    if miss > 3:
                        break        # 连续4列非深蓝:侧边栏结束
            if edge < 10:
                logger.debug("未检出深蓝侧边栏,无法锚定空白列")
                return None
            # --- 2) 内容首列(跳过侧边栏右缘的抗锯齿过渡带) ---
            x0, x1 = min(edge + 8, w), min(w, edge + 400)
            seg = band[:, x0:x1]
            if seg.shape[1] < 8:
                return None
            # 页面底色近白(三通道最小值高),文字/字母圈为深色
            ink = (seg.min(axis=2) < 180)
            col_ink = ink.sum(axis=0)
            text_left = None
            run = 0
            for x in range(seg.shape[1]):
                if col_ink[x] >= 8:
                    run += 1
                    if run >= 3:
                        text_left = x0 + x - 2
                        break
                else:
                    run = 0
            # --- 3) 空白列 ---
            offset = 20 if text_left is None else \
                min(20, max(4, (text_left - edge) // 2))
            col = edge + offset
            if not (edge + 2 <= col < w * 0.45):
                return None
            logger.info(f"探测到侧边栏右缘 x={edge},内容首列 "
                        f"x={text_left},焦点安全列 x={col}")
            return col
        except Exception as e:
            logger.warning(f"焦点安全列探测异常: {e}")
            return None

    def arrow_down(self, times: int = 10):
        """按方向键↓滚动(导航与微滚统一入口)。
        实测学习通每按一次精确滚 40px,线性可靠;滚动不改动答案,
        dry-run 下也真实执行(整页扫描依赖)。
        每次仅滚几十像素,小步多滚不跳题;需先点击内容区空白处建立焦点。"""
        with self._cursor_guard():
            self._focus_content()
            for _ in range(times):
                pyautogui.press("down")
                time.sleep(0.04)
            logger.info(f"已按 ↓×{times}")

    def arrow_up(self, times: int = 10):
        """按方向键↑滚动(批量作答阶段向上校正),同 arrow_down。"""
        with self._cursor_guard():
            self._focus_content()
            for _ in range(times):
                pyautogui.press("up")
                time.sleep(0.04)
            logger.info(f"已按 ↑×{times}")

    def press_home(self):
        """回到页面顶部(复查漏答题用)。
        需先点击内容区空白处建立焦点,否则 Home 键可能不作用于页面。
        滚动不改动答案,dry-run 下也真实执行(整页扫描依赖)。"""
        with self._cursor_guard():
            self._focus_content()
            pyautogui.press("home")
            logger.info("已按 Home 回到顶部")

    def scroll(self, clicks: int):
        """滚动滚轮。clicks>0 向下,clicks<0 向上。
        实测学习通 CEF 每格只滚约1/5题,且一次性发送多格会被合并/丢失,
        因此逐格发送并间隔0.12s。"""
        if self.dry_run:
            logger.info(f"[dry-run] 跳过滚动 {clicks} 格")
            return
        with self._cursor_guard():
            self.window.bring_to_front()
            time.sleep(0.2)
            # moveTo 到侧边栏右侧的安全空白列(同 _focus_content):悬停在
            # 选项文本上的 hover 高亮会污染选中检测的蓝像素统计
            col = self._safe_click_column()
            l, t, r, b = self.window.client_rect_screen()
            sx, sy = self.window.client_to_screen(col, (b - t) // 2)
            pyautogui.moveTo(sx, sy)
            time.sleep(0.15)
            step = 1 if clicks >= 0 else -1
            for _ in range(abs(int(clicks))):
                pyautogui.scroll(-step)          # pyautogui: 负数 = 向下
                time.sleep(0.12)
            logger.info(f"已滚动 {clicks} 格")

    def select_all(self):
        """全选当前焦点控件内容(粘贴前调用,覆盖旧文本,保证重试幂等:
        重复粘贴不会追加出双份答案)"""
        if self.dry_run:
            logger.info("[dry-run] 跳过 Ctrl+A")
            return
        pyautogui.hotkey("ctrl", "a")

    def type_text(self, text: str):
        """向当前焦点控件逐字符键入文本(填空题/简答题)。
        学习通 JS 层禁用了粘贴(onpaste 拦截),Ctrl+V 剪贴板方案无效;
        pyautogui.typewrite 只支持 ASCII。故用 SendInput KEYEVENTF_UNICODE
        逐字符发送——与真人中文输入法键入产生的事件一致(简答题本就必须
        允许输入法作答,onpaste 拦截不了键盘键入)。"""
        if self.dry_run:
            logger.info(f"[dry-run] 跳过输入: {text[:20]}")
            return
        with self._cursor_guard():
            for ch in text:
                _send_unicode_key(ch)
                time.sleep(random.uniform(0.03, 0.08))   # 拟人打字节奏
            logger.info(f"已键入文本({len(text)}字): {text[:30]}...")
