# -*- coding: utf-8 -*-
"""
输入控制器:模拟鼠标点击/输入。

采用 pyautogui(SendInput 真实点击),对 CEF/Chromium 窗口可靠;
所有点击带随机延时,防检测。
"""
import random
import time

import pyautogui

from core.controller.window import WindowCapture
from core.log import get_logger

logger = get_logger("controller.input")

# 防止 pyautogui 触发 FailSafeException 中断(鼠标移到角落时停止)
pyautogui.FAILSAFE = False


class InputController:
    def __init__(self, window: WindowCapture, action_cfg: dict):
        self.window = window
        self.cfg = action_cfg
        self.dry_run = action_cfg.get("dry_run", True)

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
        sx, sy = self.window.client_to_screen(int(x), int(y))
        self._click_screen(sx, sy, label)

    def click_options(self, option_centers: dict[str, tuple[int, int]], labels: list[str]):
        """依次点击多个选项(多选题)。labels 形如 ["A", "C"]"""
        for i, label in enumerate(labels):
            if label not in option_centers:
                logger.warning(f"选项 {label} 无坐标,跳过")
                continue
            if i > 0:
                self._sleep(self.cfg.get("option_interval", [0.3, 0.6]))
            x, y = option_centers[label]
            self.click_client(x, y, label=f"选项{label}", delay=False)

    def move_away(self):
        """把鼠标移到客户区左上角,避免悬停高亮干扰点击后的截图验证"""
        if self.dry_run:
            return
        try:
            sx, sy = self.window.client_to_screen(5, 5)
            pyautogui.moveTo(sx, sy, duration=0.2)
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
        self.window.bring_to_front()
        time.sleep(0.1)
        l, t, r, b = self.window.client_rect_screen()
        # 内容区左边距空白列(左侧导航 x<100, 题目内容 x>160, 120 为安全空白)
        sx, sy = self.window.client_to_screen(120, (b - t) // 2)
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
        self.window.bring_to_front()
        time.sleep(0.15)
        l, t, r, b = self.window.client_rect_screen()
        sx, sy = self.window.client_to_screen(120, (b - t) // 2)
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
        l, t, r, b = self.window.client_rect_screen()
        self.window.bring_to_front()
        time.sleep(0.2)
        # 取题目内容区中部一点
        sx, sy = self.window.client_to_screen(400, (b - t) // 2)
        pyautogui.moveTo(sx, sy)
        time.sleep(0.15)
        step = 1 if clicks >= 0 else -1
        for _ in range(abs(int(clicks))):
            pyautogui.scroll(-step)          # pyautogui: 负数 = 向下
            time.sleep(0.12)
        logger.info(f"已滚动 {clicks} 格")

    def type_text(self, text: str):
        """输入文本(填空题预留)"""
        if self.dry_run:
            logger.info(f"[dry-run] 跳过输入: {text[:20]}")
            return
        pyautogui.typewrite(text, interval=0.05)
