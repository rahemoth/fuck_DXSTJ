# -*- coding: utf-8 -*-
"""截图搜题:全屏冻结选区(微信式)→ OCR → LLM 答案+解析。

仅复用既有能力:Pillow ImageGrab(屏幕捕获)、core.vision.ocr(OCR)、
core.agent.llm(模型调用);覆盖层与结果窗沿用 gui.theme 方舟视觉。

多屏混合 DPI 说明:选区逻辑坐标 → 物理像素统一按主屏 devicePixelRatio
换算,各屏缩放比不一致的极端多屏场景会有偏差(主流 Windows 配置无影响)。
"""
import re

from PIL import Image, ImageGrab
from PySide6.QtCore import Qt, QRect, QPoint, Signal, QThread
from PySide6.QtGui import (
    QImage, QPainter, QPen, QColor, QPixmap, QCursor, QGuiApplication,
)
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QApplication,
)

from qfluentwidgets import (
    MessageBoxBase, SmoothScrollArea, BodyLabel, StrongBodyLabel,
    CaptionLabel, PushButton, TransparentToolButton, IndeterminateProgressBar,
    FluentIcon as FIF, qconfig,
)

from gui import theme
from gui.theme import ark, draw_corner_bracket

_ANS_RE = re.compile(r"【答案】\s*(.*?)(?=【解析】|$)", re.S)
_EXP_RE = re.compile(r"【解析】\s*(.*)$", re.S)

SYSTEM_PROMPT = (
    "你是网课答题助手。用户提供的是一道题的 OCR 识别文本,可能含识别噪声"
    "(错字、多余符号、选项错位)。请仔细理解题意后严格按以下格式输出:\n"
    "【答案】最终答案(选择题只给选项字母,判断题给 对/错,填空题给填空内容,"
    "计算/简答题给最终结果)\n"
    "【解析】分步骤讲解:先点出关键条件,再给出推理过程,必要时验证。"
    "150-400 字,使用与题目一致的语言。\n"
    "若 OCR 文本不足以确定题目(如只有选项没有题干),输出:\n"
    "【答案】无法确定\n【解析】说明缺失哪些信息、建议补截什么。"
)


# ============================== 屏幕捕获 ==============================

def capture_virtual_screen():
    """冻结整个虚拟桌面(所有屏幕并集)。

    Returns: (PIL.Image 物理像素, QRect 逻辑虚拟桌面, dpr 主屏缩放比)
    """
    img = ImageGrab.grab(bbox=None, all_screens=True)
    screens = QGuiApplication.screens()
    virt = QRect(screens[0].geometry())
    for s in screens[1:]:
        virt = virt.united(s.geometry())
    dpr = QGuiApplication.primaryScreen().devicePixelRatio()
    return img, virt, dpr


def crop_for_ocr(img: Image.Image, virt: QRect, dpr: float, sel: QRect) -> Image.Image:
    """逻辑选区 → 物理像素裁剪(相对虚拟桌面原点)"""
    x1 = int(round((sel.left() - virt.left()) * dpr))
    y1 = int(round((sel.top() - virt.top()) * dpr))
    x2 = int(round((sel.right() + 1 - virt.left()) * dpr))
    y2 = int(round((sel.bottom() + 1 - virt.top()) * dpr))
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(img.width, x2), min(img.height, y2)
    return img.crop((x1, y1, x2, y2))


def pil_to_qpixmap(img: Image.Image) -> QPixmap:
    """PIL Image → QPixmap(结果窗缩略图用)"""
    rgb = img.convert("RGB")
    qimg = QImage(rgb.tobytes("raw", "RGB"), rgb.width, rgb.height,
                  QImage.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


# ============================== 选区覆盖层 ==============================

class RegionSelector(QWidget):
    """微信式全屏选区:冻结截图压暗,拖拽框选,选区提亮 + 角标 + 尺寸标签。

    交互:拖拽框选 / 选区内拖动平移 / 四角热区缩放 / Enter 或双击确认 /
    ESC 或右键取消 / 松开后弹出 重选-确认 工具条。
    信号:confirmed(QRect 逻辑坐标) / cancelled()
    """

    confirmed = Signal(QRect)
    cancelled = Signal()

    HANDLE = 8  # 四角缩放热区(逻辑像素)

    def __init__(self, image: Image.Image, virt: QRect, dpr: float, parent=None):
        super().__init__(parent)
        self.image = image
        self.virt = virt
        self.dpr = dpr

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(virt)

        qimg = QImage(image.tobytes("raw", "RGB"), image.width, image.height,
                      QImage.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg.copy())
        self._pixmap.setDevicePixelRatio(dpr)

        self._sel: QRect | None = None
        self._mode = None          # "new" / "move" / 角名
        self._press_pos = QPoint()
        self._sel_press = QRect()

        self._toolbar = self._build_toolbar()
        qconfig.themeChangedFinished.connect(self.update)

    def _build_toolbar(self) -> QFrame:
        bar = QFrame(self)
        bar.setAttribute(Qt.WA_StyledBackground)
        t = ark()
        bar.setStyleSheet(
            f"background: {t['panel']}; border: 1px solid {t['border']};"
            f"border-radius: 6px;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        btn_re = TransparentToolButton(FIF.SYNC, bar)
        btn_re.setToolTip("重新选择")
        btn_re.clicked.connect(self._on_reselect)
        btn_ok = TransparentToolButton(FIF.ACCEPT, bar)
        btn_ok.setToolTip("确认 (Enter)")
        btn_ok.clicked.connect(self._on_confirm)
        lay.addWidget(btn_re)
        lay.addWidget(btn_ok)
        bar.hide()
        return bar

    # ---------- 对外 ----------

    def selection(self) -> QRect | None:
        return self._sel

    # ---------- 交互 ----------

    def _hit_corner(self, pos: QPoint) -> str | None:
        """返回命中的角名(tl/tr/bl/br),未命中返回 None"""
        if not self._sel:
            return None
        s = self._sel
        corners = {"tl": s.topLeft(), "tr": s.topRight(),
                   "bl": s.bottomLeft(), "br": s.bottomRight()}
        for name, c in corners.items():
            if (pos - c).manhattanLength() <= self.HANDLE:
                return name
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            if self._sel:
                self._sel = None
                self._toolbar.hide()
                self.update()
            else:
                self.cancelled.emit()
            return
        if e.button() != Qt.LeftButton:
            return
        self._press_pos = e.position().toPoint()
        corner = self._hit_corner(self._press_pos)
        if corner:
            self._mode = corner
        elif self._sel and self._sel.contains(self._press_pos):
            self._mode = "move"
            self._sel_press = QRect(self._sel)
        else:
            self._mode = "new"
            self._toolbar.hide()
        self.update()

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if self._mode is None:
            # 悬停时更新角部光标
            if self._hit_corner(pos):
                self.setCursor(Qt.SizeFDiagCursor)
            else:
                self.setCursor(Qt.CrossCursor)
            return
        if self._mode == "new":
            self._sel = QRect(self._press_pos, pos).normalized()
        elif self._mode == "move":
            delta = pos - self._press_pos
            moved = QRect(self._sel_press)
            moved.translate(delta)
            moved.moveLeft(max(self.rect().left(),
                               min(moved.left(), self.rect().right() - moved.width())))
            moved.moveTop(max(self.rect().top(),
                              min(moved.top(), self.rect().bottom() - moved.height())))
            self._sel = moved
        else:  # 角缩放
            s = QRect(self._sel)
            if self._mode == "tl":
                s.setTopLeft(pos)
            elif self._mode == "tr":
                s.setTopRight(pos)
            elif self._mode == "bl":
                s.setBottomLeft(pos)
            else:
                s.setBottomRight(pos)
            self._sel = s.normalized()
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._mode is None:
            return
        self._mode = None
        if self._sel and self._sel.width() > 4 and self._sel.height() > 4:
            self._position_toolbar()
        else:
            self._sel = None
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self._sel and self._sel.contains(e.position().toPoint()):
            self._on_confirm()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._sel and self._sel.width() > 4 and self._sel.height() > 4:
                self._on_confirm()

    def _on_reselect(self):
        self._sel = None
        self._toolbar.hide()
        self.update()

    def _on_confirm(self):
        if self._sel and self._sel.width() > 4 and self._sel.height() > 4:
            self.confirmed.emit(QRect(self._sel))

    def _position_toolbar(self):
        self._toolbar.adjustSize()
        x = self._sel.right() - self._toolbar.width()
        y = self._sel.bottom() + 8
        if y + self._toolbar.height() > self.rect().bottom():
            y = self._sel.top() - self._toolbar.height() - 8
        x = max(self.rect().left() + 4, min(x, self.rect().right() - self._toolbar.width() - 4))
        y = max(self.rect().top() + 4, y)
        self._toolbar.move(x, y)
        self._toolbar.show()
        self._toolbar.raise_()

    def showEvent(self, e):
        super().showEvent(e)
        self.activateWindow()
        self.raise_()

    # ---------- 绘制 ----------

    def paintEvent(self, e):
        t = ark()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 冻结截图
        painter.drawPixmap(0, 0, self._pixmap)
        # 整屏压暗
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if not self._sel:
            painter.setPen(QColor(t["text_muted"]))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "拖拽框选一道题 · ESC 取消")
            painter.end()
            return

        sel = QRect(self._sel)
        # 选区内重绘原图(提亮)
        painter.save()
        painter.setClipRect(sel)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.restore()
        # 边框 + 四角 L 形角标
        accent = QColor(t["accent"])
        painter.setPen(QPen(accent, 2))
        painter.drawRect(sel.adjusted(1, 1, -1, -1))
        arm = 12
        for cx, cy, dx, dy in (
                (sel.left(), sel.top(), 1, 1),
                (sel.right(), sel.top(), -1, 1),
                (sel.left(), sel.bottom(), 1, -1),
                (sel.right(), sel.bottom(), -1, -1)):
            painter.drawLine(cx, cy, cx + arm * dx, cy)
            painter.drawLine(cx, cy, cx, cy + arm * dy)
        # 尺寸标签
        label = f"{sel.width()} × {sel.height()}"
        painter.setFont(self.font())
        fm = painter.fontMetrics()
        tw, th = fm.horizontalAdvance(label) + 12, fm.height() + 6
        lx = sel.left()
        ly = sel.top() - th - 4
        if ly < self.rect().top():
            ly = sel.top() + 4
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(t["panel"]))
        painter.drawRect(QRect(lx, ly, tw, th))
        painter.setPen(QColor(t["text"]))
        painter.drawText(QRect(lx, ly, tw, th), Qt.AlignCenter, label)
        painter.end()


# ============================== 识别 + 解答 Worker ==============================

def split_reply(reply: str) -> tuple[str, str]:
    """拆【答案】/【解析】;无标记时整段作为解析返回"""
    ma = _ANS_RE.search(reply)
    me = _EXP_RE.search(reply)
    answer = ma.group(1).strip() if ma else ""
    explain = me.group(1).strip() if me else ""
    if not answer and not explain:
        return "", reply.strip()
    return answer, explain


class ScreenshotSearchWorker(QThread):
    """后台线程:OCR → LLM(沿用 EnvDetectWorker 的 QThread+Signal 模式)"""

    stage = Signal(str)
    result = Signal(dict)
    error = Signal(str)

    def __init__(self, image: Image.Image, cfg: dict):
        super().__init__()
        self.image = image
        self.cfg = cfg

    def run(self):
        try:
            self.stage.emit("正在 OCR 识别截图...")
            from core.vision.ocr import OcrEngine
            img = self.image
            threshold = float(self.cfg["ocr"].get("confidence_threshold", 0.55))
            if img.height < 60 or img.width < 300:
                # 小图沿用 executor 的放大重试先例:3× LANCZOS + 低阈值
                img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
                threshold = float(self.cfg["ocr"].get("retry_threshold", 0.3))
            blocks = OcrEngine(threshold).run(img)
            if not blocks:
                self.error.emit("未识别到文字,请重新截图(确保题目文字清晰完整)")
                return
            text = "\n".join(b.text for b in blocks)

            self.stage.emit("正在请求模型解答...")
            from core.agent.llm import LLMClient
            reply = LLMClient(self.cfg["llm"]).chat(SYSTEM_PROMPT, text)
            answer, explain = split_reply(reply)
            self.result.emit({"ocr_text": text, "answer": answer,
                              "explanation": explain, "raw": reply})
        except Exception as e:
            self.error.emit(f"识别失败: {e}")


# ============================== 结果窗 ==============================

class ScreenshotResultDialog(MessageBoxBase):
    """截图搜题结果:缩略图 / 识别文本 / 答案 / 解析,支持一键复制与继续截图"""

    continue_requested = Signal()   # 主窗口据此重新进入截图流程

    def __init__(self, thumb: QPixmap | None = None, parent=None):
        super().__init__(parent)
        self.widget.setMinimumWidth(560)
        self.yesButton.setText("关闭")
        self.yesButton.setMinimumSize(80, 36)
        self.hideCancelButton()
        self.btn_continue = PushButton("继续截图", self.buttonGroup)
        self.btn_continue.setIcon(FIF.CAMERA.icon())
        self.btn_continue.setMinimumSize(96, 36)
        self.btn_continue.setToolTip("关闭本窗口并重新框选一道题")
        self.btn_continue.clicked.connect(self._on_continue)
        self.buttonLayout.insertWidget(0, self.btn_continue, 0, Qt.AlignVCenter)
        self.btn_copy = PushButton("复制全部", self.buttonGroup)
        self.btn_copy.setIcon(FIF.COPY.icon())
        self.btn_copy.setMinimumSize(96, 36)
        self.btn_copy.clicked.connect(self._on_copy)
        self.buttonLayout.insertWidget(1, self.btn_copy, 0, Qt.AlignVCenter)

        self._answer_text = ""
        self._explain_text = ""

        # 状态行:阶段文字 + 扫描进度条
        status_row = QWidget(self.widget)
        sr = QHBoxLayout(status_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(8)
        self.lbl_stage = CaptionLabel("准备中...", status_row)
        sr.addWidget(self.lbl_stage)
        self.progress = IndeterminateProgressBar(status_row)
        self.progress.setFixedHeight(3)
        sr.addWidget(self.progress, 1)
        self.viewLayout.addWidget(status_row)

        # 内容滚动区(透明化:ScrollArea 默认白底,深色主题下必须打透)
        scroll = SmoothScrollArea(self.widget)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(320)
        content = QWidget()
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollArea::viewport { background: transparent; }"
        )
        scroll.setWidget(content)
        vbox = QVBoxLayout(content)
        vbox.setContentsMargins(4, 4, 12, 4)
        vbox.setSpacing(8)

        if thumb is not None and not thumb.isNull():
            self.lbl_thumb = QLabel(content)
            scaled = thumb.scaledToWidth(220, Qt.SmoothTransformation)
            self.lbl_thumb.setPixmap(scaled)
            vbox.addWidget(self.lbl_thumb, 0, Qt.AlignLeft)

        vbox.addWidget(CaptionLabel("识别文本", content))
        self.lbl_ocr = BodyLabel("—", content)
        self.lbl_ocr.setWordWrap(True)
        self.lbl_ocr.setTextInteractionFlags(Qt.TextSelectableByMouse)
        mono = self.lbl_ocr.font()
        mono.setFamilies(["Cascadia Mono", "Consolas", "Microsoft YaHei UI"])
        mono.setPixelSize(12)
        self.lbl_ocr.setFont(mono)
        vbox.addWidget(self.lbl_ocr)

        vbox.addWidget(CaptionLabel("答案", content))
        self.lbl_answer = StrongBodyLabel("—", content)
        self.lbl_answer.setWordWrap(True)
        self.lbl_answer.setTextInteractionFlags(Qt.TextSelectableByMouse)
        vbox.addWidget(self.lbl_answer)

        vbox.addWidget(CaptionLabel("解析", content))
        self.lbl_explain = BodyLabel("—", content)
        self.lbl_explain.setWordWrap(True)
        self.lbl_explain.setTextInteractionFlags(Qt.TextSelectableByMouse)
        vbox.addWidget(self.lbl_explain)

        self.viewLayout.addWidget(scroll)
        self._reapply_colors()
        qconfig.themeChangedFinished.connect(self._reapply_colors)
        self.progress.start()

    def _reapply_colors(self):
        t = ark()
        self.lbl_answer.setStyleSheet(f"color: {t['accent']};")

    # ---------- worker 信号槽 ----------

    def set_stage(self, text: str):
        self.lbl_stage.setText(text)

    def set_result(self, data: dict):
        self.progress.stop()
        self.progress.hide()
        self.lbl_stage.setText("完成")
        self.lbl_ocr.setText(data.get("ocr_text") or "—")
        self._answer_text = data.get("answer") or ""
        self._explain_text = data.get("explanation") or ""
        self.lbl_answer.setText(self._answer_text or "—")
        self.lbl_explain.setText(self._explain_text or data.get("raw") or "—")

    def set_error(self, message: str):
        self.progress.stop()
        self.progress.hide()
        self.lbl_stage.setText(message)
        self.lbl_stage.setStyleSheet(f"color: {ark()['error']};")
        self.btn_copy.setEnabled(False)

    def _on_copy(self):
        text = f"【答案】{self._answer_text}\n【解析】{self._explain_text}"
        QApplication.clipboard().setText(text)
        self.lbl_stage.setText("已复制到剪贴板")

    def _on_continue(self):
        """继续截图:通知主窗口重新进入截图流程,并关闭本窗口"""
        self.continue_requested.emit()
        self.reject()
