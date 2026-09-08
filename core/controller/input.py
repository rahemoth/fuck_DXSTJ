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
    def __init__(self, window: WindowCapture, action_cfg: dict):
        self.window = window
        self.cfg = action_cfg
        self.dry_run = action_cfg.get("dry_run", True)
        self._home: tuple[int, int] | None = None   # 鼠标复位点(执行开始时的位置)
        self._in_action = False                     # 嵌套动作标记(仅最外层复位)

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
            self._sleep(self.cfg.get("click_delay", [0.8, 1.8]))
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
                    self._sleep(self.cfg.get("option_interval", [0.3, 0.6]))
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
        self._sleep(self.cfg.get("next_delay", [1.0, 2.0]))

    def arrow_down(self, times: int = 10):
        """按方向键↓滚动(导航与微滚统一入口)。
        每次仅滚几十像素,小步多滚不跳题;需先点击内容区空白处建立焦点。"""
        if self.dry_run:
            logger.info(f"[dry-run] 跳过 ↓×{times}")
            return
        with self._cursor_guard():
            self.window.bring_to_front()
            time.sleep(0.1)
            l, t, r, b = self.window.client_rect_screen()
            # 焦点点击列:导航图标 x≤53,内容列缩窗时从 x≈102 起(字母圈 114~127),
            # 全屏时从 x≈435 起。x=70 在两种窗口宽度下都是空白,勿改回 120
            # (120 会压缩窗字母圈列,radio 下误点即切换已选答案)。
            sx, sy = self.window.client_to_screen(70, (b - t) // 2)
            pyautogui.click(sx, sy)
            time.sleep(0.15)
            for _ in range(times):
                pyautogui.press("down")
                time.sleep(0.04)
            logger.info(f"已按 ↓×{times}")

    def press_home(self):
        """回到页面顶部(复查漏答题用)。
        需先点击内容区空白处建立焦点,否则 Home 键可能不作用于页面。"""
        if self.dry_run:
            logger.info("[dry-run] 跳过 Home")
            return
        with self._cursor_guard():
            self.window.bring_to_front()
            time.sleep(0.15)
            l, t, r, b = self.window.client_rect_screen()
            # 同 arrow_down:x=70 是全屏/缩窗都安全的空白列(见其注释)
            sx, sy = self.window.client_to_screen(70, (b - t) // 2)
            pyautogui.click(sx, sy)
            time.sleep(0.2)
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
            l, t, r, b = self.window.client_rect_screen()
            self.window.bring_to_front()
            time.sleep(0.2)
            # moveTo 到安全空白列 x=70(同 arrow_down 注释):悬停在选项文本上
            # 的 hover 高亮会污染选中检测的蓝像素统计
            sx, sy = self.window.client_to_screen(70, (b - t) // 2)
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
