# -*- coding: utf-8 -*-
"""
窗口控制器:查找学习通PC客户端窗口、截图。

参考 MAA 的 CtrlUnit 思想:
- find(): 定位目标窗口句柄
- screenshot(): 获取窗口图像(PIL.Image),客户区坐标系
"""
import ctypes
import ctypes.wintypes as wintypes

import win32gui
import win32ui
import win32con
import win32process
from PIL import Image

from core.log import get_logger

logger = get_logger("controller.window")

# PrintWindow 的 PW_RENDERFULLCONTENT 标志(Windows 8.1+),可截取 GPU 渲染内容(CEF/Electron 窗口)
PW_RENDERFULLCONTENT = 0x00000002


class WindowNotFoundError(Exception):
    pass


class WindowCapture:
    def __init__(self, title_keywords: list[str], method: str = "printwindow"):
        self.title_keywords = title_keywords or ["学习通"]
        self.method = method
        self.hwnd = None

    # ---------- 查找窗口 ----------

    @staticmethod
    def _enum_windows() -> list[tuple[int, str]]:
        """枚举所有可见顶层窗口 (hwnd, title)"""
        result = []

        def _cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title.strip():
                    result.append((hwnd, title))
            return True

        win32gui.EnumWindows(_cb, None)
        return result

    def find(self) -> int | None:
        """按标题关键词查找窗口,返回 hwnd;找不到返回 None。
        - 排除自身进程的窗口(避免本 GUI 标题含关键词时匹配到自己)
        - 精确匹配 > 前缀匹配 > 包含匹配;同级优先标题更短的窗口
          (避免匹配到标题里恰好含关键词的编辑器/浏览器窗口)
        """
        import os

        own_pid = os.getpid()
        best = None  # (score, title_len, hwnd, title)
        for hwnd, title in self._enum_windows():
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == own_pid:
                continue
            for kw in self.title_keywords:
                if title == kw:
                    score = 0
                elif title.startswith(kw):
                    score = 1
                elif kw in title:
                    score = 2
                else:
                    continue
                candidate = (score, len(title), hwnd, title)
                if best is None or candidate < best:
                    best = candidate
        if best:
            _, _, hwnd, title = best
            self.hwnd = hwnd
            logger.info(f"找到目标窗口: [{title}] hwnd={hwnd}")
            return hwnd
        return None

    def ensure_connected(self):
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            if not self.find():
                raise WindowNotFoundError(
                    f"未找到包含关键词 {self.title_keywords} 的窗口,请先打开学习通PC客户端"
                )

    # ---------- 坐标换算 ----------

    def client_rect_screen(self) -> tuple[int, int, int, int]:
        """客户区在屏幕上的位置 (left, top, right, bottom)"""
        self.ensure_connected()
        left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
        pt_tl = wintypes.POINT(left, top)
        pt_br = wintypes.POINT(right, bottom)
        ctypes.windll.user32.ClientToScreen(self.hwnd, ctypes.byref(pt_tl))
        ctypes.windll.user32.ClientToScreen(self.hwnd, ctypes.byref(pt_br))
        return pt_tl.x, pt_tl.y, pt_br.x, pt_br.y

    def client_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """客户区坐标 -> 屏幕坐标"""
        l, t, r, b = self.client_rect_screen()
        return l + x, t + y

    def client_size(self) -> tuple[int, int]:
        l, t, r, b = self.client_rect_screen()
        return r - l, b - t

    def bring_to_front(self):
        """把窗口带到前台(pyautogui 真实点击前调用)。
        后台进程直接 SetForegroundWindow 会被 Windows 拒绝,
        用 AttachThreadInput 技巧绕过;失败时点击本身也能激活窗口"""
        self.ensure_connected()
        try:
            if win32gui.IsIconic(self.hwnd):
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            if win32gui.GetForegroundWindow() == self.hwnd:
                return
            import win32api

            fg = win32gui.GetForegroundWindow()
            fg_tid, _ = win32process.GetWindowThreadProcessId(fg)
            my_tid = win32api.GetCurrentThreadId()
            attached = False
            try:
                # AttachThreadInput 不在 win32api 里,用 ctypes 调 user32
                windll = ctypes.windll.user32
                windll.AttachThreadInput(my_tid, fg_tid, True)
                attached = True
                win32gui.SetForegroundWindow(self.hwnd)
            finally:
                if attached:
                    windll.AttachThreadInput(my_tid, fg_tid, False)
        except Exception as e:
            logger.warning(f"置前窗口失败(可忽略,点击时仍会激活): {e}")

    def post_scroll(self, notches: int, x: int, y: int):
        """向窗口投递滚轮消息(PostMessage 方式,无需窗口在前台)。
        实测学习通 CEF 窗口对真实滚轮输入响应异常,PostMessage 反而有效。
        :param notches: 滚轮格数,正数=向下,负数=向上(每格约 1/3 题)
        :param x, y: 客户区坐标(取题目内容区一点)
        """
        import win32api

        self.ensure_connected()
        sx, sy = self.client_to_screen(int(x), int(y))
        # WM_MOUSEWHEEL: delta 为负 = 向下滚动
        wparam = win32api.MAKELONG(0, -120 * int(notches))
        lparam = win32api.MAKELONG(sx, sy)
        win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)

    # ---------- 截图 ----------

    def screenshot(self) -> Image.Image:
        """截取窗口客户区。优先 PrintWindow,fallback BitBlt。"""
        self.ensure_connected()
        if self.method != "bitblt":
            img = self._screenshot_printwindow()
            if img is not None and not self._is_black(img):
                return img
            logger.warning("PrintWindow 截图黑屏/失败,回退到 BitBlt")
        img = self._screenshot_bitblt()
        if img is None or self._is_black(img):
            raise RuntimeError(
                "截图失败(窗口可能最小化或硬件加速保护)。"
                "请确保窗口未最小化;若仍失败请尝试 windows-capture 方案"
            )
        return img

    def _screenshot_printwindow(self) -> Image.Image | None:
        try:
            left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
            w, h = right - left, bottom - top
            if w <= 0 or h <= 0:
                return None
            hwnd_dc = win32gui.GetWindowDC(self.hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bmp)
            result = ctypes.windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
            if result != 1:
                win32gui.DeleteObject(bmp.GetHandle())
                save_dc.DeleteDC()
                mfc_dc.DeleteDC()
                win32gui.ReleaseDC(self.hwnd, hwnd_dc)
                return None
            bmp_info = bmp.GetInfo()
            bmp_bits = bmp.GetBitmapBits(True)
            img = Image.frombuffer(
                "RGB", (bmp_info["bmWidth"], bmp_info["bmHeight"]), bmp_bits, "raw", "BGRX", 0, 1
            )
            win32gui.DeleteObject(bmp.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hwnd_dc)
            return img
        except Exception as e:
            logger.error(f"PrintWindow 异常: {e}")
            return None

    def _screenshot_bitblt(self) -> Image.Image | None:
        try:
            left, top, right, bottom = self.client_rect_screen()
            w, h = right - left, bottom - top
            if w <= 0 or h <= 0:
                return None
            desktop_dc = win32gui.GetWindowDC(0)
            mfc_dc = win32ui.CreateDCFromHandle(desktop_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bmp)
            save_dc.BitBlt((0, 0), (w, h), mfc_dc, (left, top), win32con.SRCCOPY)
            bmp_info = bmp.GetInfo()
            bmp_bits = bmp.GetBitmapBits(True)
            img = Image.frombuffer(
                "RGB", (bmp_info["bmWidth"], bmp_info["bmHeight"]), bmp_bits, "raw", "BGRX", 0, 1
            )
            win32gui.DeleteObject(bmp.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(0, desktop_dc)
            return img
        except Exception as e:
            logger.error(f"BitBlt 异常: {e}")
            return None

    @staticmethod
    def _is_black(img: Image.Image, sample_ratio: float = 0.05) -> bool:
        """判断截图是否接近全黑(硬件加速窗口常见问题)"""
        import numpy as np

        arr = np.asarray(img.convert("L"))
        # 采样加速
        step = max(1, int(min(arr.shape) * sample_ratio))
        sampled = arr[::step, ::step]
        return float(sampled.mean()) < 5.0

    @staticmethod
    def get_process_name(hwnd: int) -> str:
        """辅助:查看窗口所属进程名(调试用)"""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        import psutil  # 可选依赖,缺失时返回空

        try:
            return psutil.Process(pid).name()
        except Exception:
            return ""


def list_candidate_windows() -> list[tuple[int, str]]:
    """调试辅助:列出所有可见窗口,方便用户确认学习通窗口标题"""
    return WindowCapture._enum_windows()
