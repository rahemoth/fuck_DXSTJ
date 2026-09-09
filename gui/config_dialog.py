# -*- coding: utf-8 -*-
"""设置对话框:API 配置、行为参数。
模型支持从服务端拉取列表选择(下拉框,亦可手输);测试连接显示往返延迟。
iOS 26 液态玻璃风格:渐变底板、半透明玻璃输入、玻璃胶囊按钮。"""
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QCheckBox,
    QLabel, QComboBox, QSpinBox, QHBoxLayout,
    QPushButton, QMessageBox,
)

from core.agent.llm import LLMClient
from core.log import get_logger

logger = get_logger("gui.config")

# iOS 26 液态玻璃(Liquid Glass)色板
_INK = "#1d1d1f"          # 主文字
_GRAY = "#6e6e73"         # 次级文字 / 小节标题
_BLUE = "#0071e3"         # 苹果蓝(主按钮/链接)

_DIALOG_QSS = f"""
* {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                 "Segoe UI", sans-serif;
}}

/* ---- 底板:蓝→薰衣草渐变"壁纸" ---- */
QDialog {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                 stop:0 #eceff7, stop:0.5 #dee6f2, stop:1 #edeef5);
}}
QLabel {{ color: {_INK}; font-size: 13px; background: transparent; border: none; }}

/* ---- 输入控件:玻璃 ---- */
QLineEdit, QSpinBox, QComboBox {{
    background: rgba(255, 255, 255, 205);
    border: 1px solid rgba(0, 0, 0, 26);
    border-top: 1px solid rgba(255, 255, 255, 235);
    border-radius: 12px; padding: 6px 10px; min-height: 20px;
    color: {_INK}; font-size: 13px;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1.5px solid {_BLUE}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: #a1a1a6; }}
QSpinBox::up-button, QSpinBox::down-button {{
    background: transparent; border: none; width: 18px;
}}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{
    background: rgba(255, 255, 255, 245);
    border: 1px solid rgba(0, 0, 0, 20);
    border-radius: 14px; padding: 4px; color: {_INK};
    selection-background-color: #eaf3fe; selection-color: {_INK};
}}

/* ---- 复选框:玻璃圆角块,选中注入蓝色 ---- */
QCheckBox {{ color: {_INK}; font-size: 13px; spacing: 8px; background: transparent; }}
QCheckBox::indicator {{
    width: 20px; height: 20px;
    border: 1px solid rgba(0, 0, 0, 32);
    border-radius: 7px; background: rgba(255, 255, 255, 195);
}}
QCheckBox::indicator:checked {{
    border-color: rgba(0, 113, 227, 200);
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 #4da2ff, stop:1 #0071e3);
    image: url(none);
}}
QCheckBox::indicator:disabled {{ background: rgba(255, 255, 255, 90); }}

/* ---- 按钮:液态玻璃胶囊(顶边高光) ---- */
QPushButton {{
    border: 1px solid rgba(255, 255, 255, 165);
    border-top: 1px solid rgba(255, 255, 255, 245);
    border-radius: 980px; padding: 8px 22px;
    font-size: 13px; font-weight: 600; color: {_INK};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 rgba(255, 255, 255, 250), stop:1 rgba(255, 255, 255, 150));
}}
QPushButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 rgba(255, 255, 255, 255), stop:1 rgba(255, 255, 255, 188));
}}
QPushButton:pressed {{ background: rgba(255, 255, 255, 135); }}
QPushButton#ghost {{ color: {_BLUE}; background: transparent; border: none; }}
QPushButton#ghost:hover {{ background: rgba(255, 255, 255, 115); }}
QPushButton#ghost:disabled {{ color: #a1a1a6; }}
QPushButton#primary {{
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 120);
    border-top: 1px solid rgba(255, 255, 255, 205);
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 rgba(120, 190, 255, 228), stop:1 rgba(0, 113, 227, 232));
}}
QPushButton#primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 rgba(140, 205, 255, 240), stop:1 rgba(0, 126, 255, 242));
}}
QPushButton#primary:pressed {{ background: rgba(0, 113, 227, 222); }}
QPushButton#primary:disabled {{ background: rgba(0, 113, 227, 100); }}
QPushButton#secondary {{
    color: {_INK};
    border: 1px solid rgba(0, 0, 0, 26);
    border-top: 1px solid rgba(255, 255, 255, 225);
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 rgba(255, 255, 255, 228), stop:1 rgba(226, 228, 234, 150));
}}
QPushButton#secondary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 rgba(255, 255, 255, 245), stop:1 rgba(226, 228, 234, 185));
}}
QPushButton#secondary:disabled {{ color: #a1a1a6; background: rgba(255, 255, 255, 70); }}
"""


def _section(text: str) -> QLabel:
    """苹果式小节标题:灰色小字加粗,上下留白"""
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {_GRAY}; font-size: 12px; font-weight: 600; "
        "background: transparent; border: none; padding: 0;")
    return lbl


class _TaskBridge(QObject):
    """后台线程 → GUI 线程信号桥(网络请求不得阻塞 GUI)"""
    # task: "models" / "ping"; ok: 是否成功; payload: 数据或错误信息
    finished = Signal(str, bool, object)


class ConfigDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("设置")
        self.setMinimumWidth(540)
        self.setStyleSheet(_DIALOG_QSS)

        self._bridge = _TaskBridge()
        self._bridge.finished.connect(self._on_task_finished)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(14)
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight)

        # ---- LLM 配置 ----
        form.addRow(_section("模型 API · OpenAI 兼容"))
        self.base_url = QLineEdit(cfg["llm"]["base_url"])
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        form.addRow("Base URL", self.base_url)

        self.api_key = QLineEdit(cfg["llm"]["api_key"])
        self.api_key.setEchoMode(QLineEdit.Password)
        form.addRow("API Key", self.api_key)

        # 模型:可编辑下拉框 + 获取列表按钮
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setCurrentText(cfg["llm"]["model"])
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        model_row.addWidget(self.model, stretch=1)
        self.btn_models = QPushButton("获取模型列表")
        self.btn_models.setObjectName("ghost")
        self.btn_models.clicked.connect(self.on_fetch_models)
        model_row.addWidget(self.btn_models)
        self.btn_ping = QPushButton("测试连接")
        self.btn_ping.setObjectName("ghost")
        self.btn_ping.clicked.connect(self.on_test_connection)
        model_row.addWidget(self.btn_ping)
        form.addRow("模型", model_row)

        self.net_status = QLabel("")
        self.net_status.setWordWrap(True)
        form.addRow("", self.net_status)

        # ---- 行为配置 ----
        form.addRow(_section("行为"))
        self.dry_run = QCheckBox("dry-run 模式(只识别和请求答案,不实际点击)")
        self.dry_run.setChecked(cfg["action"]["dry_run"])
        form.addRow("", self.dry_run)

        self.click_delay = self._range_spin(cfg["action"]["click_delay"])
        form.addRow("点击延时(秒,随机区间)", self.click_delay)

        # ---- 窗口配置 ----
        form.addRow(_section("窗口"))
        self.title_keywords = QLineEdit(",".join(cfg["window"]["title_keywords"]))
        self.title_keywords.setPlaceholderText("学习通")
        form.addRow("窗口标题关键词(逗号分隔)", self.title_keywords)

        # ---- 网页版配置 ----
        form.addRow(_section("网页版"))
        self.default_browser = QComboBox()
        self.default_browser.addItem("(未选择)", "")
        self.default_browser.addItem("Edge", "edge")
        self.default_browser.addItem("Chrome", "chrome")
        saved_browser = cfg.get("web", {}).get("default_browser", "")
        idx = self.default_browser.findData(saved_browser)
        self.default_browser.setCurrentIndex(idx if idx >= 0 else 0)
        self.default_browser.setToolTip(
            "网页版模式将拉起该浏览器的程序专用实例(独立配置,不影响日常浏览器),\n"
            "首次使用需在弹出的窗口中登录学习通一次,之后登录态保留")
        form.addRow("默认浏览器", self.default_browser)

        self.cdp_port = QSpinBox()
        self.cdp_port.setRange(1024, 65535)
        self.cdp_port.setValue(int(cfg.get("web", {}).get("cdp_port", 9222)))
        form.addRow("调试端口", self.cdp_port)

        self.launch_browser = QCheckBox("插件模式自动拉起专用浏览器")
        self.launch_browser.setChecked(cfg.get("web", {}).get("launch_browser", True))
        self.launch_browser.setToolTip(
            "勾选:点开始时自动拉起带插件的程序专用浏览器实例;\n"
            "取消:使用你日常的浏览器(需手动装一次插件,详见开始后日志指引:\n"
            "扩展管理页开启开发人员模式 → 加载解压缩的扩展 → 选 webextension 目录)")
        form.addRow("", self.launch_browser)

        layout.addLayout(form)
        layout.addStretch()

        # 底部按钮:取消(灰) + 保存(苹果蓝胶囊)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("secondary")
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_cancel)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("primary")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self.accept)
        buttons.addWidget(self.btn_save)
        layout.addLayout(buttons)

    # ---------- 网络任务(后台线程) ----------

    def _llm_cfg(self) -> dict:
        """以对话框当前输入构造 LLM 配置(未保存也能测试)"""
        return {
            "base_url": self.base_url.text().strip(),
            "api_key": self.api_key.text().strip(),
            "model": self.model.currentText().strip(),
            "temperature": self.cfg["llm"].get("temperature", 0.1),
            "timeout": 15,
        }

    def _run_task(self, task: str, fn):
        """后台线程执行网络任务,完成经信号桥回 GUI"""
        self.btn_models.setEnabled(False)
        self.btn_ping.setEnabled(False)

        def worker():
            try:
                payload = fn()
                self._bridge.finished.emit(task, True, payload)
            except Exception as e:
                self._bridge.finished.emit(task, False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def on_fetch_models(self):
        """获取服务端模型列表填充下拉框"""
        if not self._check_llm_inputs():
            return
        self.net_status.setText("正在获取模型列表...")
        self.net_status.setStyleSheet(f"color: {_GRAY};")
        self._run_task("models", lambda: LLMClient(self._llm_cfg()).list_models())

    def on_test_connection(self):
        """测试模型连接并显示往返延迟"""
        if not self._check_llm_inputs():
            return
        self.net_status.setText("正在测试连接...")
        self.net_status.setStyleSheet(f"color: {_GRAY};")
        self._run_task("ping", lambda: LLMClient(self._llm_cfg()).ping())

    def _check_llm_inputs(self) -> bool:
        if not self.base_url.text().strip() or not self.api_key.text().strip():
            QMessageBox.warning(self, "缺少配置", "请先填写 Base URL 和 API Key")
            return False
        if not self.model.currentText().strip() and self.sender() is self.btn_ping:
            QMessageBox.warning(self, "缺少配置", "请先填写或选择模型名")
            return False
        return True

    def _on_task_finished(self, task: str, ok: bool, payload):
        self.btn_models.setEnabled(True)
        self.btn_ping.setEnabled(True)
        if task == "port":
            self._on_port_finished(ok, payload)
            return
        if task == "models":
            if not ok:
                self.net_status.setText(f"获取失败: {payload}")
                self.net_status.setStyleSheet("color: #ff3b30;")
                return
            current = self.model.currentText()
            self.model.clear()
            self.model.addItems(payload)
            if current in payload:
                self.model.setCurrentText(current)
            self.net_status.setText(f"获取到 {len(payload)} 个模型")
            self.net_status.setStyleSheet("color: #34c759;")
        else:  # ping
            if not ok:
                self.net_status.setText(f"连接失败: {payload}")
                self.net_status.setStyleSheet("color: #ff3b30;")
                return
            elapsed, reply = payload
            self.net_status.setText(
                f"连接成功,延迟 {elapsed * 1000:.0f} ms,模型回复: {reply[:30]}")
            self.net_status.setStyleSheet("color: #34c759;")

    # ---------- 收集 ----------

    @staticmethod
    def _range_spin(range_list) -> QLineEdit:
        """延时范围以文本框呈现,格式: 最小,最大"""
        return QLineEdit(f"{range_list[0]},{range_list[1]}")

    def _parse_range(self, widget: QLineEdit, default: list) -> list:
        try:
            parts = widget.text().split(",")
            values = [float(p.strip()) for p in parts if p.strip()]
            if len(values) == 2 and values[0] <= values[1]:
                return values
            if len(values) == 1:
                return [values[0], values[0]]
        except ValueError:
            pass
        return default

    def collect(self) -> dict:
        """返回各节配置的更新字典"""
        keywords = [k.strip() for k in self.title_keywords.text().split(",") if k.strip()]
        return {
            "llm": {
                "base_url": self.base_url.text().strip(),
                "api_key": self.api_key.text().strip(),
                "model": self.model.currentText().strip(),
            },
            "action": {
                "dry_run": self.dry_run.isChecked(),
                "click_delay": self._parse_range(self.click_delay, self.cfg["action"]["click_delay"]),
            },
            "window": {
                "title_keywords": keywords or ["学习通"],
            },
            "web": {
                "default_browser": self.default_browser.currentData(),
                "cdp_port": self.cdp_port.value(),
                "launch_browser": self.launch_browser.isChecked(),
            },
        }
