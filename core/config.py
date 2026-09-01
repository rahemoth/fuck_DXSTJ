# -*- coding: utf-8 -*-
"""配置加载模块"""
from pathlib import Path
import copy

import yaml

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"

# 默认配置(与 config.yaml 同步,缺失项用默认值兜底)
DEFAULT_CONFIG = {
    "window": {
        "title_keywords": ["学习通"],
        "capture_method": "printwindow",
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.1,
        "timeout": 60,
        "max_retries": 1,
    },
    "action": {
        "dry_run": True,
        "click_delay": [0.8, 1.8],
        "next_delay": [1.0, 2.0],
        "option_interval": [0.3, 0.6],
        "scroll_clicks": 2,
        "scroll_wait": 0.8,
    },
    "ocr": {
        "confidence_threshold": 0.55,
    },
    "web": {
        "default_browser": "",           # 默认浏览器: "" 未选择 / edge / chrome(程序拉起该浏览器的专用实例)
        "cdp_port": 9222,                # 专用浏览器调试端口(网页版模式)
        "url_keywords": ["chaoxing", "mooc"],  # 学习通页面 URL 特征
        "wait_page_timeout": 180,        # 等待用户打开做题页的超时(秒)
        "q_delay": [3.0, 8.0],           # 每题间隔随机秒数(防检测)
        "opt_delay": [0.5, 1.5],         # 选项间点击随机秒数
    },
    "log": {
        "save_to_file": True,
        "dir": "logs",
    },
}


def _merge(default: dict, user: dict) -> dict:
    """递归合并:用户配置覆盖默认值"""
    result = copy.deepcopy(default)
    for k, v in (user or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


class Config:
    """全局配置单例"""

    _instance = None
    _data = None

    def __init__(self):
        raise RuntimeError("请使用 Config.get() 获取实例")

    @classmethod
    def get(cls) -> "Config":
        if cls._instance is None:
            cls._instance = cls.__new__(cls)
            cls._instance.reload()
        return cls._instance

    def reload(self):
        user = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
        self._data = _merge(DEFAULT_CONFIG, user)

    def save(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)

    @property
    def data(self) -> dict:
        return self._data

    def __getitem__(self, key):
        return self._data[key]

    def update(self, section: str, values: dict):
        """更新某节配置并保存"""
        if section not in self._data or not isinstance(self._data[section], dict):
            self._data[section] = {}
        self._data[section].update(values)
        self.save()


def load_config() -> dict:
    """便捷函数:加载完整配置字典"""
    return Config.get().data
