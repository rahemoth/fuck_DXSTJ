# -*- coding: utf-8 -*-
"""设置对话框:API 配置、行为参数"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QCheckBox,
    QDoubleSpinBox, QDialogButtonBox, QLabel,
)


class ConfigDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)

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

        self.model = QLineEdit(cfg["llm"]["model"])
        form.addRow("模型名", self.model)

        # ---- 行为配置 ----
        form.addRow(QLabel("—— 行为 ——"))
        self.dry_run = QCheckBox("dry-run 模式(只识别和请求答案,不实际点击)")
        self.dry_run.setChecked(cfg["action"]["dry_run"])
        form.addRow("", self.dry_run)

        self.click_delay = self._range_spin(cfg["action"]["click_delay"])
        form.addRow("点击延时(秒,随机区间)", self.click_delay)

        self.next_delay = self._range_spin(cfg["action"]["next_delay"])
        form.addRow("翻页延时(秒,随机区间)", self.next_delay)

        # ---- 窗口配置 ----
        form.addRow(QLabel("—— 窗口 ——"))
        self.title_keywords = QLineEdit(",".join(cfg["window"]["title_keywords"]))
        self.title_keywords.setPlaceholderText("学习通")
        form.addRow("窗口标题关键词(逗号分隔)", self.title_keywords)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _range_spin(range_list) -> str:
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
                "model": self.model.text().strip(),
            },
            "action": {
                "dry_run": self.dry_run.isChecked(),
                "click_delay": self._parse_range(self.click_delay, self.cfg["action"]["click_delay"]),
                "next_delay": self._parse_range(self.next_delay, self.cfg["action"]["next_delay"]),
            },
            "window": {
                "title_keywords": keywords or ["学习通"],
            },
        }
