# -*- coding: utf-8 -*-
"""
主窗口(Fluent Widgets + 方舟视觉):
- FluentWidget 基座:主题感知自绘标题栏、纯色底 + 极淡网格
- 顶部:切角分段控件 / InfoBadge 状态胶囊 / 琥珀扫描进度条 / 切角按钮
- 中部:切角题目卡(题型 Badge + 题干 + 选项 + 答案块)、三张指标卡
- 底部:日志卡(级别着色、自动滚动、清空)

线程模型:GUI 主线程 + Worker(QThread),Executor 事件经信号转发回 GUI,保持不变。
"""
import re

from PySide6.QtCore import (
    Qt, QThread, Signal, QObject, QTimer, QElapsedTimer, QRectF,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidgetItem,
    QPushButton, QButtonGroup, QSizePolicy,
)

from qfluentwidgets import (
    FluentWidget, FluentIcon as FIF, FluentIconBase, drawIcon,
    TransparentPushButton, TransparentToolButton, InfoBadge, InfoBar,
    IndeterminateProgressBar, SimpleCardWidget, SubtitleLabel, BodyLabel,
    StrongBodyLabel, CaptionLabel, CheckBox, TextEdit, TreeWidget,
    PushButton, MessageBoxBase, qconfig, setTheme, Theme,
)

from core.config import Config
from core.log import setup_logging, set_gui_callback, get_logger
from core.pipeline.executor import Executor, ExecutorEvent
from gui import theme
from gui.config_dialog import ConfigDialog
from gui.theme import ark, paint_chamfer, draw_corner_bracket, THEME_NAMES, THEME_ORDER

TYPE_NAMES = {"single": "单选题", "multiple": "多选题", "judge": "判断题",
              "fill": "填空题", "short_answer": "简答题"}
TYPE_CHIP_KEYS = {"single": "chip_single", "multiple": "chip_multiple",
                  "judge": "chip_judge", "fill": "chip_fill",
                  "short_answer": "chip_short"}

_DONE_RE = re.compile(r"完成\s*(\d+)\s*题")
_FAIL_RE = re.compile(r"失败\s*(\d+)\s*题")

MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Microsoft YaHei UI"]


class LogBridge(QObject):
    """日志信号桥:工作线程发日志 → 信号(自动排队)→ GUI 主线程追加"""
    log_signal = Signal(str)


class Worker(QThread):
    """承载 Executor 主循环的工作线程(客户端 OCR / 网页 CDP / 网页插件)"""
    event = Signal(str, dict)  # kind, data

    def __init__(self, cfg: dict, mode: int = 0):
        super().__init__()
        if mode == 1:
            from core.web.driver import WebExecutor
            self.executor = WebExecutor(cfg, emit=self._on_event)
        elif mode == 2:
            from core.web.ext_executor import ExtensionExecutor
            self.executor = ExtensionExecutor(cfg, emit=self._on_event)
        else:
            self.executor = Executor(cfg, emit=self._on_event)

    def _on_event(self, e: ExecutorEvent):
        self.event.emit(e.kind, e.data)

    def run(self):
        self.executor.run()

    def stop(self):
        self.executor.stop()


# ============================== 方舟自绘控件 ==============================

class _ArkCard(SimpleCardWidget):
    """切角卡片:面板色填充 + 1px 边框 + 左上 L 形角标(替代 Fluent 圆角卡)。
    bar_enabled=True 时左缘绘制 3px 青绿状态竖条(答案块用)。"""

    def __init__(self, parent=None, bracket_accent=False, bracket=True):
        super().__init__(parent)
        self._bracket_accent = bracket_accent
        self._bracket = bracket
        self.bar_enabled = False
        qconfig.themeChangedFinished.connect(self.update)

    def paintEvent(self, e):
        t = ark()
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        paint_chamfer(painter, rect, 11, QColor(t["panel"]), QColor(t["border"]))
        if self.bar_enabled:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(t["success"]))
            painter.drawRect(QRectF(1, 12, 3, rect.height() - 24))
        if self._bracket:
            bracket = t["accent"] if self._bracket_accent else t["border"]
            draw_corner_bracket(painter, 9, 9, 8, QColor(bracket))
        painter.end()


class _ArkButton(QPushButton):
    """切角按钮。kind:
    - primary:琥珀实心底 + 深色字
    - secondary:透明底 + 1px 边框,悬停边框转琥珀
    - danger:错误色描边与文字
    - segment:分段控件项(可选中,选中琥珀底深色字)
    """

    def __init__(self, text="", icon=None, kind="secondary", parent=None):
        super().__init__(text, parent)
        self._icon = icon
        self._kind = kind
        self._icon_cache: dict = {}
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(32)
        qconfig.themeChangedFinished.connect(self.update)

    def _colors(self):
        t = ark()
        hover = self.underMouse() and self.isEnabled()
        down = self.isDown()
        if not self.isEnabled():
            return QColor(t["panel"]), QColor(t["divider"]), QColor(t["text_disabled"])
        if self._kind == "primary":
            fill = t["accent_pressed"] if down else (t["accent_hover"] if hover else t["accent"])
            return QColor(fill), None, QColor(t["accent_text"])
        if self._kind == "danger":
            border = QColor(t["error"]) if hover else QColor(t["error"])
            fill = QColor(t["error"])
            fill.setAlpha(46 if hover else 24)
            return fill, border, QColor(t["error"])
        if self._kind == "segment":
            if self.isChecked():
                return QColor(t["accent"]), None, QColor(t["accent_text"])
            fill = QColor(t["overlay"]) if hover else QColor(Qt.transparent)
            return fill, None, QColor(t["text"] if hover else t["text_muted"])
        # secondary
        border = QColor(t["accent"]) if hover else QColor(t["border"])
        fill = QColor(t["overlay"]) if hover else QColor(Qt.transparent)
        return fill, border, QColor(t["text"])

    def _colored_icon(self, color: QColor):
        key = color.name()
        icon = self._icon_cache.get(key)
        if icon is None and isinstance(self._icon, FluentIconBase):
            icon = self._icon.colored(color, color)
            self._icon_cache[key] = icon
        return icon or self._icon

    def paintEvent(self, e):
        fill, border, text_color = self._colors()
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        paint_chamfer(painter, rect, 6, fill, border)

        text = self.text()
        metrics = QFontMetrics(self.font())
        text_w = metrics.horizontalAdvance(text)
        icon_w = 16 if self._icon else 0
        gap = 6 if self._icon and text else 0
        total = icon_w + gap + text_w
        x = (self.width() - total) / 2

        if self._icon:
            icon_rect = QRectF(x, (self.height() - 16) / 2, 16, 16)
            drawIcon(self._colored_icon(text_color), painter, icon_rect)
            x += icon_w + gap
        painter.setPen(text_color)
        painter.drawText(QRectF(x, 0, text_w + 1, self.height()),
                         Qt.AlignVCenter | Qt.AlignLeft, text)
        painter.end()


class _ArkSegmented(QWidget):
    """切角分段控件:三项互斥,选中琥珀底深色字,项间 1px 竖线分隔。
    暴露 currentIndex()/setCurrentIndex(),与 QComboBox 用法兼容。"""

    def __init__(self, items: list[str], parent=None):
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        metrics = QFontMetrics(self.font())
        for i, text in enumerate(items):
            btn = _ArkButton(text, kind="segment")
            btn.setCheckable(True)
            btn.setMinimumWidth(metrics.horizontalAdvance(text) + 32)
            self._group.addButton(btn, i)
            lay.addWidget(btn)
        self._group.button(0).setChecked(True)
        qconfig.themeChangedFinished.connect(self.update)

    def currentIndex(self) -> int:
        return self._group.checkedId()

    def setCurrentIndex(self, index: int):
        btn = self._group.button(index)
        if btn:
            btn.setChecked(True)

    def paintEvent(self, e):
        # 段间 1px 竖分隔线
        painter = QPainter(self)
        painter.setPen(QColor(ark()["divider"]))
        buttons = self._group.buttons()
        for i in range(1, len(buttons)):
            x = buttons[i].x()
            painter.drawLine(x, 7, x, self.height() - 7)
        painter.end()


class _LogView(TextEdit):
    """按级别着色的日志视图:解析 'HH:MM:SS [LEVEL] msg' 逐行上色。
    用 QTextCursor + QTextCharFormat 写入,绕过 HTML span 与 Fluent 样式表的冲突,
    主题切换后所有日志行颜色自动跟随,对比度可控。"""

    LINE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s*(.*)$")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("暂无日志")
        self.document().setMaximumBlockCount(2000)
        font = QFont()
        font.setFamilies(MONO_FAMILIES)
        font.setPixelSize(12)
        self.setFont(font)
        self._auto_scroll = True
        # 主题切换时整文档重绘颜色
        qconfig.themeChangedFinished.connect(self._recolor_all)
        # 用 (已渲染文本, 级别) 存原始内容,重绘时直接用
        self._lines: list[tuple[str, str]] = []  # [(raw_text, level_or_''), ...]

    def set_auto_scroll(self, on: bool):
        self._auto_scroll = on

    @staticmethod
    def _fmt(color_hex: str) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color_hex))
        return fmt

    def _append_colored(self, raw: str):
        """解析 raw 行,用 QTextCursor 逐段写入"""
        t = ark()
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.End)
        m = self.LINE_RE.match(raw)
        if m:
            ts, level, msg = m.groups()
            level_u = level.upper()
            if level_u in ("ERROR", "CRITICAL"):
                msg_color = t["error"]
            elif level_u == "WARNING":
                msg_color = t["warning"]
            else:
                msg_color = t["text"]
            # 时间戳:用 text_muted 替代 text_disabled,提升对比度
            cursor.insertText(ts + " ", self._fmt(t["text_muted"]))
            # LEVEL 方括号:同 text_muted
            cursor.insertText(f"[{level_u}] ", self._fmt(t["text_muted"]))
            # 消息主体
            cursor.insertText(msg, self._fmt(msg_color))
        else:
            cursor.insertText(raw, self._fmt(t["text"]))
        cursor.insertBlock()

    def _recolor_all(self):
        """主题切换后重建整份文档,颜色全部重新取当前主题的"""
        self.clear()
        for raw, _ in self._lines:
            self._append_colored(raw)

    def append_line(self, text: str):
        self._lines.append((text, ""))
        # 只保留最近 2000 条
        if len(self._lines) > 2000:
            self._lines.pop(0)
        self._append_colored(text)
        if self._auto_scroll:
            sb = self.verticalScrollBar()
            sb.setValue(sb.maximum())


class EnvDetectDialog(MessageBoxBase):
    """环境检测结果展示:树形列表(IDE / Agent / Python / 工具链)。
    基于 MessageBoxBase,与设置弹窗同一套 Fluent 视觉,深浅主题自动跟随。"""

    SECTION_TITLES = {
        "ides": "IDE / 编辑器",
        "agents": "AI 编程 Agent",
        "pythons": "Python",
        "toolchains": "语言工具链",
    }

    def __init__(self, result: dict, parent=None):
        super().__init__(parent)
        self.yesButton.setText("关闭")
        self.hideCancelButton()
        self.yesButton.setMinimumSize(80, 36)
        self.widget.setMinimumWidth(640)

        self.tree = TreeWidget(self.widget)
        self.tree.setHeaderLabels(["名称", "版本 / 来源", "路径"])
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 260)
        self.tree.setMinimumHeight(380)
        for section, items in result.items():
            title = self.SECTION_TITLES.get(section, section)
            group = QTreeWidgetItem([f"{title} ({len(items)})"])
            group.setFlags(Qt.ItemIsEnabled)  # 仅分组标题,不可选
            if not items:
                empty = QTreeWidgetItem(["(未检测到)"])
                empty.setFlags(Qt.NoItemFlags)
                group.addChild(empty)
            for it in items:
                ver = it.get("version") or f"来源: {it['source']}"
                child = QTreeWidgetItem([it["name"], ver, it["path"]])
                group.addChild(child)
            group.setExpanded(True)
            self.tree.addTopLevelItem(group)
        self.viewLayout.addWidget(self.tree)


class EnvDetectWorker(QThread):
    """后台线程执行环境检测(含版本探测的 subprocess,不阻塞 GUI)"""
    finished_signal = Signal(dict)

    def run(self):
        from core.env.detector import detect_all
        try:
            self.finished_signal.emit(detect_all())
        except Exception as e:
            self.logger_error = str(e)
            self.finished_signal.emit({})


class WebConnectWorker(QThread):
    """后台线程执行网页版连接测试(CDP 检测,不阻塞 GUI)"""
    result_signal = Signal(bool, str)

    def __init__(self, web_cfg: dict):
        super().__init__()
        self.web_cfg = web_cfg

    def run(self):
        from core.web.driver import test_connection
        try:
            self.result_signal.emit(*test_connection(self.web_cfg))
        except Exception as e:
            self.result_signal.emit(False, f"网页版连接测试异常: {e}")


class MainWindow(FluentWidget):
    def __init__(self):
        super().__init__()
        setup_logging()
        self.logger = get_logger("gui")

        self.setWindowTitle("fuck_DXSTJ - 学习通自动做题")
        self.resize(960, 700)
        self.setMinimumSize(820, 600)
        # 方舟视觉要求纯色底 + 自绘网格,关闭云母避免穿透干扰
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor(theme.ARK_LIGHT["bg"], theme.ARK_DARK["bg"])
        self.worker: Worker | None = None

        # 运行统计与计时
        self._done_count = 0
        self._fail_count = 0
        self._elapsed = QElapsedTimer()
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)

        self._theme_mode = str(
            Config.get().data.get("ui", {}).get("theme", "dark")).lower()
        if self._theme_mode not in THEME_ORDER:
            self._theme_mode = "dark"

        self._build_ui()
        qconfig.themeChangedFinished.connect(self._on_fluent_theme_changed)

        # 日志经信号桥跨线程安全转发到 GUI
        self._log_bridge = LogBridge()
        self._log_bridge.log_signal.connect(self.log_view.append_line)
        set_gui_callback(self._log_bridge.log_signal.emit)

    # ---------- UI 构建 ----------

    def _build_ui(self):
        top_margin = max(self.titleBar.height(), 32) + 8

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, top_margin, 0, 0)
        outer.addStretch(1)
        container = QWidget(self)
        container.setMaximumWidth(1100)
        outer.addWidget(container, 1)
        outer.addStretch(1)

        root = QVBoxLayout(container)
        root.setContentsMargins(16, 0, 16, 16)
        root.setSpacing(12)

        self._build_title_bar_buttons()

        # 顶部控制栏
        top = QHBoxLayout()
        top.setSpacing(8)
        self.mode_combo = _ArkSegmented(
            ["客户端(OCR)", "网页版(浏览器)", "网页版(插件注入)"])
        self.mode_combo.setToolTip(
            "客户端:OCR 识别学习通 PC 客户端\n"
            "网页版(浏览器):CDP 直连浏览器,DOM 读题 + 程序点击\n"
            "网页版(插件注入):程序自动给专用浏览器装插件,插件读题点击,\n"
            "主程序只负责调模型给答案(iframe 兼容性最好)")
        top.addWidget(self.mode_combo)

        self.status_badge = InfoBadge("● 未连接", self)
        self._set_status("idle", "● 未连接")
        top.addWidget(self.status_badge)
        top.addStretch(1)

        self.btn_connect = TransparentPushButton(FIF.LINK, "测试连接", self)
        self.btn_connect.clicked.connect(self.on_connect)
        top.addWidget(self.btn_connect)

        self.btn_start = _ArkButton("开始", FIF.PLAY_SOLID, kind="primary")
        self.btn_start.clicked.connect(self.on_start)
        top.addWidget(self.btn_start)

        self.btn_stop = _ArkButton("停止", FIF.CLOSE, kind="danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)
        top.addWidget(self.btn_stop)
        root.addLayout(top)

        # 运行中琥珀扫描条
        self.progress = IndeterminateProgressBar(self)
        self.progress.setFixedHeight(3)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # 题目预览卡
        preview = _ArkCard(self, bracket_accent=True)
        pv = QVBoxLayout(preview)
        pv.setContentsMargins(16, 14, 16, 14)
        pv.setSpacing(8)

        head = QHBoxLayout()
        self.lbl_type = InfoBadge("待识别", self)
        self.lbl_type.setCustomBackgroundColor(
            theme.ARK_LIGHT["text_disabled"], theme.ARK_DARK["text_disabled"])
        head.addWidget(self.lbl_type)
        head.addStretch(1)
        pv.addLayout(head)

        self.lbl_stem = StrongBodyLabel("尚未识别到题目,点击「开始」后在此展示", preview)
        self.lbl_stem.setWordWrap(True)
        self.lbl_stem.setMinimumHeight(20)
        self.lbl_stem.setTextInteractionFlags(Qt.TextSelectableByMouse)
        stem_font = QFont()
        stem_font.setPixelSize(14)
        stem_font.setBold(True)
        self.lbl_stem.setFont(stem_font)
        pv.addWidget(self.lbl_stem)

        self.options_box = QWidget(preview)
        self.options_layout = QVBoxLayout(self.options_box)
        self.options_layout.setContentsMargins(0, 0, 0, 0)
        self.options_layout.setSpacing(6)
        self.options_box.setVisible(False)
        pv.addWidget(self.options_box)

        # 答案块:切角卡 + 左侧状态竖条
        self.answer_block = _ArkCard(preview, bracket=False)
        self._answer_active = False
        ab = QHBoxLayout(self.answer_block)
        ab.setContentsMargins(14, 8, 14, 8)
        ab.setSpacing(8)
        self.answer_block.setMinimumHeight(36)
        ans_title = CaptionLabel("模型答案", self.answer_block)
        ab.addWidget(ans_title)
        self.lbl_answer = StrongBodyLabel("—", self.answer_block)
        self.lbl_answer.setWordWrap(True)
        self.lbl_answer.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._set_answer_color(theme.ARK_DARK["text_muted"])
        ab.addWidget(self.lbl_answer, 1)
        pv.addWidget(self.answer_block)
        root.addWidget(preview)

        # 指标卡行
        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        card_done, self.lbl_done = self._metric_card("COMPLETED / 完成题数")
        card_fail, self.lbl_fail = self._metric_card("FAILED / 失败题数")
        card_time, self.lbl_elapsed = self._metric_card("ELAPSED / 用时")
        self.lbl_elapsed.setText("00:00")
        metrics.addWidget(card_done, 1)
        metrics.addWidget(card_fail, 1)
        metrics.addWidget(card_time, 1)
        root.addLayout(metrics)

        # 日志卡
        log_card = _ArkCard(self)
        lv = QVBoxLayout(log_card)
        lv.setContentsMargins(16, 10, 16, 12)
        lv.setSpacing(8)
        log_head = QHBoxLayout()
        log_title = SubtitleLabel("日志", log_card)
        log_head.addWidget(log_title)
        log_head.addStretch(1)
        self.chk_autoscroll = CheckBox("自动滚动", log_card)
        self.chk_autoscroll.setChecked(True)
        self.chk_autoscroll.toggled.connect(self._on_autoscroll_toggled)
        log_head.addWidget(self.chk_autoscroll)
        self.btn_clear_log = TransparentToolButton(FIF.DELETE, log_card)
        self.btn_clear_log.setToolTip("清空日志")
        self.btn_clear_log.clicked.connect(self.log_view_clear)
        log_head.addWidget(self.btn_clear_log)
        lv.addLayout(log_head)

        self.log_view = _LogView(log_card)
        self.log_view.setMinimumHeight(80)
        lv.addWidget(self.log_view)
        root.addWidget(log_card, stretch=1)

    def _build_title_bar_buttons(self):
        """网课助手(预留)/ 环境检测 / 设置 / 主题切换放入标题栏右侧(释放内容区宽度)"""
        self.btn_course = TransparentToolButton(FIF.EDUCATION, self.titleBar)
        self.btn_course.setToolTip("网课助手(预留)")
        self.btn_course.setEnabled(False)   # 功能未实现,置灰
        # 未来实现时: self.btn_course.clicked.connect(self.on_course_assistant)

        self.btn_env = TransparentToolButton(FIF.SEARCH, self.titleBar)
        self.btn_env.setToolTip("环境检测")
        self.btn_env.clicked.connect(self.on_env_detect)

        self.btn_config = TransparentToolButton(FIF.SETTING, self.titleBar)
        self.btn_config.setToolTip("设置")
        self.btn_config.clicked.connect(self.on_config)

        self.btn_theme = TransparentToolButton(FIF.PALETTE, self.titleBar)
        self.btn_theme.setToolTip(f"主题:{THEME_NAMES[self._theme_mode]}(点击切换)")
        self.btn_theme.clicked.connect(self.on_theme_toggle)

        layout = self.titleBar.hBoxLayout
        insert_at = layout.count() - 1  # min/max/close 按钮组之前
        for btn in (self.btn_course, self.btn_env, self.btn_config, self.btn_theme):
            layout.insertWidget(insert_at, btn, 0, Qt.AlignVCenter)
            insert_at += 1

    def _set_answer_color(self, hex_color: str):
        self.lbl_answer.setStyleSheet(f"color: {hex_color};")

    @staticmethod
    def _metric_card(label_text: str):
        card = _ArkCard()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(2)
        value = BodyLabel("0", card)
        vf = QFont()
        vf.setFamilies(MONO_FAMILIES)
        vf.setPixelSize(30)
        vf.setBold(True)
        value.setFont(vf)
        label = CaptionLabel(label_text, card)
        lf = QFont()
        lf.setPixelSize(11)
        lf.setLetterSpacing(QFont.AbsoluteSpacing, 2)
        label.setFont(lf)
        v.addWidget(value)
        v.addWidget(label)
        return card, value

    # ---------- 背景网格 ----------

    def paintEvent(self, e):
        super().paintEvent(e)
        # 极淡网格线(与背景差值 ≤ 8/255,间距 32px)
        painter = QPainter(self)
        painter.setPen(QColor(ark()["grid"]))
        top = self.titleBar.height()
        w, h = self.width(), self.height()
        x = 0
        while x <= w:
            painter.drawLine(x, top, x, h)
            x += 32
        y = top
        while y <= h:
            painter.drawLine(0, y, w, y)
            y += 32
        painter.end()

    # ---------- 状态与运行视觉 ----------

    def _set_status(self, state: str, text: str):
        """更新连接状态胶囊:idle / ok / warn / err"""
        colors = {
            "idle": (theme.ARK_LIGHT["text_disabled"], theme.ARK_DARK["text_disabled"]),
            "ok": (theme.ARK_LIGHT["success"], theme.ARK_DARK["success"]),
            "warn": (theme.ARK_LIGHT["warning"], theme.ARK_DARK["warning"]),
            "err": (theme.ARK_LIGHT["error"], theme.ARK_DARK["error"]),
        }
        light, dark = colors.get(state, colors["idle"])
        self.status_badge.setText(text)
        self.status_badge.setCustomBackgroundColor(light, dark)

    def _set_running_visuals(self, running: bool):
        self.progress.setVisible(running)
        if running:
            self.progress.start()
        else:
            self.progress.stop()

    def _on_tick(self):
        secs = self._elapsed.elapsed() // 1000
        self.lbl_elapsed.setText(f"{secs // 60:02d}:{secs % 60:02d}")

    def _on_autoscroll_toggled(self, on: bool):
        self.log_view.set_auto_scroll(on)

    def log_view_clear(self):
        self.log_view.clear()

    # ---------- 主题切换 ----------

    def _on_fluent_theme_changed(self):
        """任何地方(设置弹窗 / 本窗口按钮)调 setTheme 后同步主题模式并重绘"""
        mode = str(Config.get().data.get("ui", {}).get("theme", "dark")).lower()
        if mode not in THEME_ORDER:
            mode = "dark"
        self._theme_mode = mode
        self.btn_theme.setToolTip(f"主题:{THEME_NAMES[self._theme_mode]}(点击切换)")
        # Fluent 重刷会清掉答案标签的自定义色,按当前状态重设
        self._set_answer_active(self._answer_active)
        self.update()  # 网格背景重绘

    def on_theme_toggle(self):
        """dark → light → auto 循环,立即生效并持久化。
        注意先写 Config 再 setTheme:setTheme 发射的 themeChangedFinished
        会触发 _on_fluent_theme_changed 回读 Config,顺序反了会读到旧值。"""
        idx = THEME_ORDER.index(self._theme_mode)
        self._theme_mode = THEME_ORDER[(idx + 1) % len(THEME_ORDER)]
        Config.get().update("ui", {"theme": self._theme_mode})
        fluent_theme = {"dark": Theme.DARK, "light": Theme.LIGHT,
                        "auto": Theme.AUTO}[self._theme_mode]
        setTheme(fluent_theme)
        InfoBar.success("主题已切换",
                        f"已切换到{THEME_NAMES[self._theme_mode]}",
                        duration=2000, parent=self)

    # ---------- 按钮事件 ----------

    def on_connect(self):
        if self.mode_combo.currentIndex() >= 1:
            self.on_connect_web()
            return
        from core.controller.window import WindowCapture

        cfg = Config.get()
        win = WindowCapture(cfg["window"]["title_keywords"], cfg["window"]["capture_method"])
        if win.find():
            try:
                img = win.screenshot()
                self._set_status("ok", "● 已连接")
                self.logger.info(f"连接成功,截图尺寸 {img.size}")
            except Exception as e:
                self._set_status("warn", "● 窗口已找到,但截图失败")
                self.logger.error(f"截图失败: {e}")
        else:
            self._set_status("err", "● 未找到学习通窗口")
            self.logger.warning("未找到学习通窗口,请打开学习通PC客户端后重试")

    def on_config(self):
        cfg = Config.get()
        dlg = ConfigDialog(cfg.data, self)
        if dlg.exec():
            updates = dlg.collect()
            for section, values in updates.items():
                cfg.update(section, values)
            cfg.reload()
            self.logger.info("配置已保存")
            if cfg["action"]["dry_run"]:
                self.logger.info("当前为 dry-run 模式:只识别和请求答案,不会点击")

    def on_connect_web(self):
        """网页版测试连接:后台线程检测 CDP 端口与学习通页面"""
        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("测试中...")
        self.logger.info("网页版:测试浏览器调试端口连接 ...")
        self._web_worker = WebConnectWorker(Config.get()["web"])
        self._web_worker.result_signal.connect(self._on_web_connect_result)
        self._web_worker.start()

    def _on_web_connect_result(self, ok: bool, msg: str):
        self.btn_connect.setEnabled(True)
        self.btn_connect.setText("测试连接")
        if ok:
            self._set_status("ok", "● 已连接(网页版)")
            self.logger.info(f"网页版连接测试通过: {msg}")
        else:
            self._set_status("warn", "● 网页版未就绪")
            self.logger.warning(f"网页版连接测试失败: {msg}")

    # ---------- 环境检测 ----------

    def on_env_detect(self):
        """后台线程检测本机 IDE/Agent/Python/工具链,结果弹窗展示"""
        self.btn_env.setEnabled(False)
        self.logger.info("开始检测本机开发环境...")
        self._env_worker = EnvDetectWorker()
        self._env_worker.finished_signal.connect(self._on_env_detected)
        self._env_worker.start()

    def _on_env_detected(self, result: dict):
        self.btn_env.setEnabled(True)
        if not result:
            self.logger.error(f"环境检测失败: {getattr(self._env_worker, 'logger_error', '未知错误')}")
            return
        dlg = EnvDetectDialog(result, self)
        dlg.exec()

    def on_start(self):
        cfg = Config.get()
        if not cfg["llm"]["api_key"] or "xxxx" in cfg["llm"]["api_key"]:
            InfoBar.warning("缺少配置", "请先在【设置】中填写 API Key 和模型信息",
                            duration=3000, parent=self)
            return
        if cfg["action"]["dry_run"]:
            self.logger.info("dry-run 模式已开启:不会实际点击")

        mode = self.mode_combo.currentIndex()   # 0 客户端 / 1 网页CDP / 2 网页插件
        if mode == 0:
            # 客户端模式:重连窗口
            from core.controller.window import WindowCapture
            win = WindowCapture(cfg["window"]["title_keywords"], cfg["window"]["capture_method"])
            if not win.find():
                InfoBar.warning("未找到窗口", "未找到学习通窗口,请先打开学习通PC客户端",
                                duration=3000, parent=self)
                return
        elif mode == 1:
            self.logger.info("网页版模式:将连接调试端口的浏览器(未启动会自动拉起)")
        else:
            self.logger.info("插件模式:将启动本地桥并拉起带插件的专用浏览器")

        self.worker = Worker(cfg.data, mode=mode)
        self.worker.event.connect(self.on_worker_event)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_connect.setEnabled(False)

        # 重置统计并开始计时
        self._done_count = 0
        self._fail_count = 0
        self.lbl_done.setText("0")
        self.lbl_fail.setText("0")
        self.lbl_elapsed.setText("00:00")
        self._elapsed.start()
        self._tick_timer.start()
        self._set_running_visuals(True)

    def on_stop(self):
        if self.worker:
            self.worker.stop()

    def on_worker_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_connect.setEnabled(True)
        self._tick_timer.stop()
        self._on_tick()
        self._set_running_visuals(False)

    def on_worker_event(self, kind: str, data: dict):
        if kind == "question":
            qtype = data["qtype"]
            self.lbl_type.setText(TYPE_NAMES.get(qtype, qtype))
            chip = theme.ARK_DARK.get(TYPE_CHIP_KEYS.get(qtype, ""),
                                      theme.ARK_DARK["text_disabled"])
            chip_light = theme.ARK_LIGHT.get(TYPE_CHIP_KEYS.get(qtype, ""),
                                             theme.ARK_LIGHT["text_disabled"])
            self.lbl_type.setCustomBackgroundColor(chip_light, chip)
            self.lbl_stem.setText(data["stem"] or "(空)")
            self._set_options(data.get("options") or {})
            self._set_answer_active(False)
            self.lbl_answer.setText("—")
        elif kind == "answer":
            self.lbl_answer.setText(" / ".join(data["answer"]))
            self._set_answer_active(True)
            self._done_count += 1
            self.lbl_done.setText(str(self._done_count))
        elif kind == "done":
            summary = data.get("summary", "")
            m = _DONE_RE.search(summary)
            if m:
                self.lbl_done.setText(m.group(1))
            m = _FAIL_RE.search(summary)
            if m:
                self.lbl_fail.setText(m.group(1))
            self.logger.info(summary)
            self._set_answer_active(False)
            self.lbl_answer.setText("—")
        elif kind == "error":
            InfoBar.error("运行错误", data.get("message", "未知错误"),
                          duration=5000, parent=self)

    def _set_answer_active(self, active: bool):
        """答案块状态:有答案时文字/左边条转青绿,空态为中性灰"""
        t = ark()
        self._answer_active = active
        self.answer_block.bar_enabled = active
        self._set_answer_color(t["success"] if active else t["text_muted"])
        self.answer_block.update()

    def _set_options(self, options: dict):
        """选项逐行 BodyLabel 排列"""
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not options:
            self.options_box.setVisible(False)
            return
        for k, v in options.items():
            row = BodyLabel(f"{k}. {v}", self.options_box)
            row.setWordWrap(True)
            row.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.options_layout.addWidget(row)
        self.options_box.setVisible(True)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        event.accept()
