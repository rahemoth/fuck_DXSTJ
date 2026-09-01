# -*- coding: utf-8 -*-
"""设置对话框:API 配置、行为参数。
模型支持从服务端拉取列表选择(下拉框,亦可手输);测试连接显示往返延迟。"""
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QCheckBox,
    QDialogButtonBox, QLabel, QComboBox, QSpinBox, QHBoxLayout,
    QPushButton, QMessageBox,
)

from core.agent.llm import LLMClient
from core.log import get_logger

logger = get_logger("gui.config")


class _TaskBridge(QObject):
    """后台线程 → GUI 线程信号桥(网络请求不得阻塞 GUI)"""
    # task: "models" / "ping"; ok: 是否成功; payload: 数据或错误信息
    finished = Signal(str, bool, object)


class ConfigDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)

        self._bridge = _TaskBridge()
        self._bridge.finished.connect(self._on_task_finished)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # ---- LLM 配置 ----
        form.addRow(QLabel("—— 模型 API(OpenAI 兼容)——"))
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
        model_row.addWidget(self.model, stretch=1)
        self.btn_models = QPushButton("获取模型列表")
        self.btn_models.clicked.connect(self.on_fetch_models)
        model_row.addWidget(self.btn_models)
        self.btn_ping = QPushButton("测试连接")
        self.btn_ping.clicked.connect(self.on_test_connection)
        model_row.addWidget(self.btn_ping)
        form.addRow("模型", model_row)

        self.net_status = QLabel("")
        self.net_status.setWordWrap(True)
        form.addRow("", self.net_status)

        # ---- 行为配置 ----
        form.addRow(QLabel("—— 行为 ——"))
        self.dry_run = QCheckBox("dry-run 模式(只识别和请求答案,不实际点击)")
        self.dry_run.setChecked(cfg["action"]["dry_run"])
        form.addRow("", self.dry_run)

        self.scroll_steps = QSpinBox()
        self.scroll_steps.setRange(1, 20)
        self.scroll_steps.setValue(int(cfg["action"].get("fine_scroll_steps", 3)))
        self.scroll_steps.setToolTip("方向键↓每次滚动按几下。约50px/次,次数越多单次滚动幅度越大")
        form.addRow("每次滚动按↓次数", self.scroll_steps)

        self.click_delay = self._range_spin(cfg["action"]["click_delay"])
        form.addRow("点击延时(秒,随机区间)", self.click_delay)

        self.next_delay = self._range_spin(cfg["action"]["next_delay"])
        form.addRow("翻页延时(秒,随机区间)", self.next_delay)

        # ---- 窗口配置 ----
        form.addRow(QLabel("—— 窗口 ——"))
        self.title_keywords = QLineEdit(",".join(cfg["window"]["title_keywords"]))
        self.title_keywords.setPlaceholderText("学习通")
        form.addRow("窗口标题关键词(逗号分隔)", self.title_keywords)

        # ---- 网页版配置 ----
        form.addRow(QLabel("—— 网页版 ——"))
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

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
        self.net_status.setStyleSheet("color: gray;")
        self._run_task("models", lambda: LLMClient(self._llm_cfg()).list_models())

    def on_test_connection(self):
        """测试模型连接并显示往返延迟"""
        if not self._check_llm_inputs():
            return
        self.net_status.setText("正在测试连接...")
        self.net_status.setStyleSheet("color: gray;")
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
                self.net_status.setStyleSheet("color: red;")
                return
            current = self.model.currentText()
            self.model.clear()
            self.model.addItems(payload)
            if current in payload:
                self.model.setCurrentText(current)
            self.net_status.setText(f"获取到 {len(payload)} 个模型")
            self.net_status.setStyleSheet("color: green;")
        else:  # ping
            if not ok:
                self.net_status.setText(f"连接失败: {payload}")
                self.net_status.setStyleSheet("color: red;")
                return
            elapsed, reply = payload
            self.net_status.setText(
                f"连接成功,延迟 {elapsed * 1000:.0f} ms,模型回复: {reply[:30]}")
            self.net_status.setStyleSheet("color: green;")

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
                "fine_scroll_steps": self.scroll_steps.value(),
                "click_delay": self._parse_range(self.click_delay, self.cfg["action"]["click_delay"]),
                "next_delay": self._parse_range(self.next_delay, self.cfg["action"]["next_delay"]),
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
