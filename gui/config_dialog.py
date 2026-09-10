# -*- coding: utf-8 -*-
"""设置对话框(Fluent 版):API 配置、行为参数、外观主题。
基于 MessageBoxBase(无边框 + 遮罩),表单按 SettingCardGroup 卡片分组,
内容区用 SmoothScrollArea 承载。模型支持从服务端拉取列表;测试连接显示往返延迟。"""
import threading

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    MessageBoxBase, SettingCardGroup, SettingCard, SwitchSettingCard,
    BodyLabel, LineEdit, PasswordLineEdit, EditableComboBox, ComboBox,
    SpinBox, PushButton, InfoBar, SmoothScrollArea,
    FluentIcon as FIF, qconfig, setTheme, setThemeColor, Theme,
)

from core.agent.llm import LLMClient
from core.config import Config
from core.log import get_logger
from gui import theme as gui_theme

logger = get_logger("gui.config")

# net_status 状态色:不硬编码,统一从 ark() 调色板按当前主题动态取色
_STATUS_COLOR_KEYS = {"busy": "text_muted", "ok": "success", "err": "error"}


class _TaskBridge(QObject):
    """后台线程 → GUI 线程信号桥(网络请求不得阻塞 GUI)"""
    # task: "models" / "ping"; ok: 是否成功; payload: 数据或错误信息
    finished = Signal(str, bool, object)


def _localize_switch(card: SwitchSettingCard):
    """SwitchSettingCard 的开关文字默认是英文 Off/On,本地化为 关闭/开启"""
    sb = card.switchButton
    sb.setText("开启" if sb.isChecked() else "关闭")
    sb.checkedChanged.connect(
        lambda checked: sb.setText("开启" if checked else "关闭"))


class ConfigDialog(MessageBoxBase):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg

        # 底部按钮:最小尺寸 + 按钮区内边距,防止文字截断
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.yesButton.setMinimumSize(80, 36)
        self.cancelButton.setMinimumSize(80, 36)
        self.buttonLayout.setContentsMargins(20, 12, 20, 20)
        self.widget.setMinimumWidth(640)

        self._bridge = _TaskBridge()
        self._bridge.finished.connect(self._on_task_finished)

        # 记录 net_status 当前级别;主题切换时按 ark() 重新取色应用
        self._net_status_level = "busy"
        qconfig.themeChangedFinished.connect(self._reapply_status_color)

        scroll = SmoothScrollArea(self.widget)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(420)
        content = QWidget()
        # ScrollArea 的 viewport / content 默认白底,必须设 transparent 让父容器
        # (MessageBoxBase 的 widget 已经跟随主题)透出来;否则深色模式下仍然一片白
        content.setObjectName("configScrollContent")
        scroll.viewport().setObjectName("configScrollViewport")
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollArea::viewport { background: transparent; }"
        )
        scroll.setWidget(content)
        vbox = QVBoxLayout(content)
        vbox.setContentsMargins(4, 4, 12, 4)
        vbox.setSpacing(12)

        # ---- 分组 1:模型 API ----
        g1 = SettingCardGroup("模型 API(OpenAI 兼容)", content)
        self.base_url = LineEdit()
        self.base_url.setText(cfg["llm"]["base_url"])
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        self.base_url.setMinimumWidth(280)
        self._add_card(g1, FIF.LINK, "Base URL", "OpenAI 兼容接口地址", self.base_url)

        self.api_key = PasswordLineEdit()
        self.api_key.setText(cfg["llm"]["api_key"])
        self.api_key.setMinimumWidth(280)
        self._add_card(g1, FIF.CERTIFICATE, "API Key", "密钥仅保存在本地 config.yaml", self.api_key)

        self.model = EditableComboBox()
        self.model.setText(cfg["llm"]["model"])
        model_row = QWidget()
        h = QHBoxLayout(model_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(self.model, 1)
        self.btn_models = PushButton("获取模型列表", model_row)
        self.btn_models.clicked.connect(self.on_fetch_models)
        h.addWidget(self.btn_models)
        self.btn_ping = PushButton("测试连接", model_row)
        self.btn_ping.clicked.connect(self.on_test_connection)
        h.addWidget(self.btn_ping)
        self._add_card(g1, FIF.ROBOT, "模型", "可下拉选择或手动输入模型名", model_row)

        # 状态文字:跨全宽,跟在分组卡片底部
        self.net_status = BodyLabel("")
        self.net_status.setWordWrap(True)
        g1.vBoxLayout.addWidget(self.net_status)
        vbox.addWidget(g1)

        # ---- 分组 2:行为 ----
        g2 = SettingCardGroup("行为", content)
        self.dry_run = SwitchSettingCard(
            FIF.PAUSE, "dry-run 模式", "只识别和请求答案,不实际点击", parent=g2)
        self.dry_run.setChecked(cfg["action"]["dry_run"])
        _localize_switch(self.dry_run)
        g2.addSettingCard(self.dry_run)

        # 做题模式:long_screenshot=整页长图扫描批量作答 / per_question=逐题
        self.batch_mode = SwitchSettingCard(
            FIF.TILES, "长截图批量做题",
            "开启=整页扫描后批量作答(页面不适合时自动回退逐题);"
            "关闭=逐题识别作答(旧模式)", parent=g2)
        self.batch_mode.setChecked(
            cfg["action"].get("answer_mode", "long_screenshot") != "per_question")
        _localize_switch(self.batch_mode)
        g2.addSettingCard(self.batch_mode)

        self.click_delay = self._range_edit(cfg["action"]["click_delay"])
        self._add_card(g2, FIF.STOP_WATCH, "点击延时(秒)", "随机区间,格式:最小,最大", self.click_delay)
        vbox.addWidget(g2)

        # ---- 分组 3:窗口 ----
        g3 = SettingCardGroup("窗口", content)
        self.title_keywords = LineEdit()
        self.title_keywords.setText(",".join(cfg["window"]["title_keywords"]))
        self.title_keywords.setPlaceholderText("学习通")
        self.title_keywords.setMinimumWidth(280)
        self._add_card(g3, FIF.APPLICATION, "窗口标题关键词", "多个关键词用逗号分隔", self.title_keywords)
        vbox.addWidget(g3)

        # ---- 分组 4:网页版 ----
        g4 = SettingCardGroup("网页版", content)
        self.default_browser = ComboBox()
        self.default_browser.addItem("(未选择)", userData="")
        self.default_browser.addItem("Edge", userData="edge")
        self.default_browser.addItem("Chrome", userData="chrome")
        saved_browser = cfg.get("web", {}).get("default_browser", "")
        idx = self.default_browser.findData(saved_browser)
        self.default_browser.setCurrentIndex(idx if idx >= 0 else 0)
        self._add_card(g4, FIF.GLOBE, "默认浏览器",
                       "网页版模式拉起该浏览器的专用实例,登录态保留", self.default_browser)

        self.cdp_port = SpinBox()
        self.cdp_port.setRange(1024, 65535)
        self.cdp_port.setValue(int(cfg.get("web", {}).get("cdp_port", 9222)))
        self._add_card(g4, FIF.CODE, "调试端口", "专用浏览器 CDP 调试端口", self.cdp_port)

        self.launch_browser = SwitchSettingCard(
            FIF.SPEED_HIGH, "插件模式自动拉起专用浏览器",
            "取消勾选则使用日常浏览器(需手动装一次插件)", parent=g4)
        self.launch_browser.setChecked(cfg.get("web", {}).get("launch_browser", True))
        _localize_switch(self.launch_browser)
        g4.addSettingCard(self.launch_browser)
        vbox.addWidget(g4)

        # ---- 分组 5:外观 ----
        g5 = SettingCardGroup("外观", content)
        self.theme_combo = ComboBox()
        self.theme_combo.addItem("深色模式", userData="dark")
        self.theme_combo.addItem("浅色模式", userData="light")
        self.theme_combo.addItem("跟随系统", userData="auto")
        saved_theme = str(cfg.get("ui", {}).get("theme", "dark")).lower()
        idx = self.theme_combo.findData(saved_theme)
        self.theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._add_card(g5, FIF.BRUSH, "主题模式", "深色 / 浅色 / 跟随系统,选择后立即生效", self.theme_combo)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        vbox.addWidget(g5)

        self.viewLayout.addWidget(scroll)

    @staticmethod
    def _add_card(group, icon, title, content, widget):
        """SettingCard 右侧嵌入自定义控件(不带 configItem 的裸卡片模式)"""
        card = SettingCard(icon, title, content, group)
        card.hBoxLayout.addWidget(widget, 0, Qt.AlignRight)
        card.hBoxLayout.addSpacing(16)
        group.addSettingCard(card)
        return card

    # ---------- 状态文字着色(颜色随主题动态取色) ----------

    def _set_status(self, text: str, level: str):
        self._net_status_level = level
        self.net_status.setText(text)
        self._reapply_status_color()

    def _reapply_status_color(self):
        key = _STATUS_COLOR_KEYS.get(self._net_status_level, "text_muted")
        self.net_status.setStyleSheet(f"color: {gui_theme.ark()[key]};")

    # ---------- 主题切换:立即生效,持久化在 collect() 的 ui 节 ----------

    def _on_theme_changed(self, index: int):
        mode = self.theme_combo.itemData(index)
        fluent_theme = {"dark": Theme.DARK, "light": Theme.LIGHT}.get(mode, Theme.AUTO)
        # 先持久化:setTheme 发射的 themeChangedFinished 会触发主窗口回读
        # Config 同步 _theme_mode,顺序反了主窗口会读到旧值。
        # Config 为单例,主窗口与本对话框共享同一份数据。
        Config.get().update("ui", {"theme": mode})
        Config.get().reload()
        # setThemeColor 内部会再刷一次样式表,必须先调;
        # setTheme 最后发 themeChangedFinished,状态色在那里重新应用才不会被覆盖
        setThemeColor(gui_theme.ACCENT_HEX)
        setTheme(fluent_theme)
        self.update()

    # ---------- 网络任务(后台线程) ----------

    def _llm_cfg(self) -> dict:
        """以对话框当前输入构造 LLM 配置(未保存也能测试)"""
        return {
            "base_url": self.base_url.text().strip(),
            "api_key": self.api_key.text().strip(),
            "model": self.model.text().strip(),
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
        self._set_status("正在获取模型列表...", "busy")
        self._run_task("models", lambda: LLMClient(self._llm_cfg()).list_models())

    def on_test_connection(self):
        """测试模型连接并显示往返延迟"""
        if not self._check_llm_inputs():
            return
        self._set_status("正在测试连接...", "busy")
        self._run_task("ping", lambda: LLMClient(self._llm_cfg()).ping())

    def _check_llm_inputs(self) -> bool:
        if not self.base_url.text().strip() or not self.api_key.text().strip():
            InfoBar.warning("缺少配置", "请先填写 Base URL 和 API Key",
                            duration=3000, parent=self)
            return False
        if not self.model.text().strip() and self.sender() is self.btn_ping:
            InfoBar.warning("缺少配置", "请先填写或选择模型名",
                            duration=3000, parent=self)
            return False
        return True

    def _on_task_finished(self, task: str, ok: bool, payload):
        self.btn_models.setEnabled(True)
        self.btn_ping.setEnabled(True)
        if task == "models":
            if not ok:
                self._set_status(f"获取失败: {payload}", "err")
                return
            current = self.model.text()
            self.model.clear()
            self.model.addItems(payload)
            if current in payload:
                self.model.setText(current)
            self._set_status(f"获取到 {len(payload)} 个模型", "ok")
        else:  # ping
            if not ok:
                self._set_status(f"连接失败: {payload}", "err")
                return
            elapsed, reply = payload
            self._set_status(
                f"连接成功,延迟 {elapsed * 1000:.0f} ms,模型回复: {reply[:30]}", "ok")

    # ---------- 收集 ----------

    @staticmethod
    def _range_edit(range_list) -> LineEdit:
        """延时范围以文本框呈现,格式: 最小,最大"""
        edit = LineEdit()
        edit.setText(f"{range_list[0]},{range_list[1]}")
        edit.setMinimumWidth(180)
        return edit

    def _parse_range(self, widget: LineEdit, default: list) -> list:
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
        """返回各节配置的更新字典(ui 节新增:主题随保存持久化)"""
        keywords = [k.strip() for k in self.title_keywords.text().split(",") if k.strip()]
        return {
            "llm": {
                "base_url": self.base_url.text().strip(),
                "api_key": self.api_key.text().strip(),
                "model": self.model.text().strip(),
            },
            "action": {
                "dry_run": self.dry_run.isChecked(),
                "answer_mode": "long_screenshot" if self.batch_mode.isChecked()
                               else "per_question",
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
            "ui": {
                "theme": self.theme_combo.currentData(),
            },
        }
