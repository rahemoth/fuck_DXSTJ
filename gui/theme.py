# -*- coding: utf-8 -*-
"""主题模块:方舟调色板(供 QPainter 自绘取色)+ 主题应用 + 切角绘制辅助。

组件外观由 PySide6-Fluent-Widgets 的 setTheme 驱动;本模块只提供:
- ARK_DARK / ARK_LIGHT:明日方舟风配色令牌(深色冷工业 + 琥珀黄强调)
- apply_theme(app):按配置应用 dark/light/auto 主题并设定琥珀主色
- ark():按当前主题返回调色板,自绘控件经 qconfig.themeChangedFinished 重绘
- chamfer_path / draw_corner_bracket:切角矩形与 L 形角标的 QPainter 辅助
"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

# ============================== 方舟调色板 ==============================

ARK_DARK = {
    # 背景分层
    "bg": "#0d1017",          # 窗口底(近黑冷蓝)
    "panel": "#161a23",       # 卡片/面板
    "overlay": "#1d2230",     # 悬浮层
    "border": "#2a3140",
    "divider": "#232a37",
    # 文字
    "text": "#e8ecf4",
    "text_muted": "#8b95a8",
    "text_disabled": "#4d5668",
    # 琥珀主色
    "accent": "#f9b233",
    "accent_hover": "#ffc857",
    "accent_pressed": "#d99a26",
    "accent_text": "#171301",  # 琥珀底上的深色文字
    # 状态色(低饱和)
    "success": "#3ddc97",
    "warning": "#f5853f",      # 警告用橙,与主色黄区分
    "error": "#ff5c5c",
    "info": "#38bdf8",
    # 网格线(与背景差值 ≤ 8/255)
    "grid": "#141926",
    # 题型 chip
    "chip_single": "#f9b233",
    "chip_multiple": "#38bdf8",
    "chip_judge": "#3ddc97",
    "chip_fill": "#f5853f",
    "chip_short": "#c084fc",
}

ARK_LIGHT = {
    "bg": "#eef0f4",
    "panel": "#ffffff",
    "overlay": "#f4f6fa",
    "border": "#d4d9e2",
    "divider": "#e2e6ee",
    "text": "#1d2029",
    "text_muted": "#5a6070",
    "text_disabled": "#9aa1b0",
    # 浅色下黄色降饱和,保证对比
    "accent": "#d99a26",
    "accent_hover": "#f9b233",
    "accent_pressed": "#b57f1c",
    "accent_text": "#ffffff",
    "success": "#0f9d63",
    "warning": "#d96a1f",
    "error": "#d64550",
    "info": "#1280b8",
    "grid": "#e7eaf0",
    "chip_single": "#d99a26",
    "chip_multiple": "#1280b8",
    "chip_judge": "#0f9d63",
    "chip_fill": "#d96a1f",
    "chip_short": "#9a5bd0",
}

THEME_NAMES = {"dark": "深色模式", "light": "浅色模式", "auto": "跟随系统"}
THEME_ORDER = ["dark", "light", "auto"]

ACCENT_HEX = "#f9b233"


def ark() -> dict:
    """按当前 Fluent 主题返回方舟调色板"""
    from qfluentwidgets import isDarkTheme
    return ARK_DARK if isDarkTheme() else ARK_LIGHT


def apply_theme(app) -> None:
    """读取配置并应用主题:setTheme(dark/light/auto) + 琥珀主色"""
    from PySide6.QtGui import QFont
    from qfluentwidgets import Theme, setTheme, setThemeColor

    from core.config import Config

    mode = str(Config.get()["ui"]["theme"]).lower()
    theme = {"dark": Theme.DARK, "light": Theme.LIGHT}.get(mode, Theme.AUTO)
    setTheme(theme)
    setThemeColor(ACCENT_HEX)
    app.setFont(QFont("Microsoft YaHei UI", 9))


# ============================== 切角绘制辅助 ==============================

def chamfer_path(rect: QRectF, cut: float) -> QPainterPath:
    """切角矩形路径:右上角与左下角各切去 cut 像素的 45° 直角"""
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    path = QPainterPath(QPointF(x + cut, y))
    path.lineTo(x + w - cut, y)      # 上边
    path.lineTo(x + w, y + cut)      # 右上切角
    path.lineTo(x + w, y + h)        # 右边
    path.lineTo(x + cut, y + h)      # 下边(至左下切角)
    path.lineTo(x, y + h - cut)      # 左下切角
    path.lineTo(x, y)                # 左边
    path.closeSubpath()
    return path


def paint_chamfer(painter: QPainter, rect: QRectF, cut: float,
                  fill: QColor, border: QColor | None = None) -> None:
    """填充 + 描边一个切角矩形"""
    painter.setRenderHint(QPainter.Antialiasing)
    path = chamfer_path(rect, cut)
    painter.setPen(Qt.NoPen)
    painter.setBrush(fill)
    painter.drawPath(path)
    if border is not None:
        painter.setPen(QPen(border, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)


def draw_corner_bracket(painter: QPainter, x: float, y: float,
                        size: float, color: QColor) -> None:
    """在 (x, y) 处绘制 L 形角标(两条短线,方舟卡片左上角装饰)"""
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(color, 2, Qt.SolidLine, Qt.SquareCap))
    painter.drawLine(QPointF(x, y), QPointF(x + size, y))
    painter.drawLine(QPointF(x, y), QPointF(x, y + size))
