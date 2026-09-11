# -*- coding: utf-8 -*-
"""配置加载模块 —— 全项目唯一配置来源。

运行时只读 config.yaml 一个文件(原 core/resource/roi.json 的布局标定
已并入本节 roi 节)。DEFAULT_CONFIG 是代码侧唯一的完整 schema 与默认值
来源:加载时与用户配置深合并,再做合法性校验。

业务代码一律直接索引 cfg[section][key],禁止再写字面量默认值;
键缺失/非法由本模块在加载期统一显式报错。
"""
from pathlib import Path
import copy
import logging
import numbers

import yaml

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"

logger = logging.getLogger("config")

# 预留节:功能未上线但允许用户提前配置,不做 schema 校验与未知告警
_RESERVED_SECTIONS = {"course"}

# 默认配置 = 完整配置 schema(与 config.example.yaml 叶子键严格一致,
# 由 tests/test_config_schema.py 守护)
DEFAULT_CONFIG = {
    "window": {
        "title_keywords": ["学习通"],
        "capture_method": "printwindow",          # printwindow / bitblt
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.1,
        "timeout": 60,
        "max_retries": 1,
        "concurrency": 1,        # LLM 并发求解线程数(批量模式整页扫描后并发取答案)
    },
    "action": {
        "dry_run": True,         # true=只模拟不真实点击
        "answer_mode": "long_screenshot",  # long_screenshot / per_question
        "click_delay": [0.8, 1.8],
        "option_interval": [0.3, 0.6],
        "next_delay": [1.0, 2.0],
        "page_wait": 1.0,        # 导航/Home 跳转后等待页面稳定(秒)
        "scan_step_ratio": 0.7,  # 整页扫描每帧步长(占视口高比例)
        "scan_settle": 0.7,      # 整页扫描相邻帧滚动后的等待(秒)
        "max_scan_frames": 120,  # 扫描帧数安全上限
        "fine_scroll_steps": 3,  # 方向键↓微滚按键数
        "fine_scroll_wait": 0.6,  # 微滚后等待页面稳定(秒)
        "verify_wait": 0.6,      # 点击后等待选中高亮生效(秒)
    },
    "ocr": {
        "confidence_threshold": 0.55,
        "retry_threshold": 0.3,  # 识别不完整时降阈值重识别
    },
    "web": {
        "default_browser": "",            # "" 未选择 / edge / chrome
        "cdp_port": 9222,                 # 专用浏览器调试端口
        "ext_bridge_port": 9876,          # 插件模式本地桥 HTTP 端口
        "launch_browser": True,           # 插件模式自动拉起带插件的专用浏览器
        "url_keywords": ["chaoxing", "mooc"],
        "wait_page_timeout": 180,         # 等待打开做题页的超时(秒)
        "q_delay": [3.0, 8.0],            # 每题间隔随机秒数(防检测)
        "opt_delay": [0.5, 1.5],          # 选项间点击随机秒数
    },
    "ui": {
        "theme": "dark",                  # dark / light / auto
    },
    "log": {
        "save_to_file": True,
        "dir": "logs",
    },
    # 学习通 PC 客户端(1080×1920 窗口)布局几何标定:UI 改版只调本节
    "roi": {
        "content_region": [100, 0, 1000, 99999],
        "answer_card_width": 190,
        "option_indent": 30,
        "option_max_offset_x": 250,
        "option_line_gap": 35,
        "option_row_gap": 49,
        "stem_option_gap": 110,
        "bottom_margin": 60,
        "option_labels": ["A", "B", "C", "D", "E", "F"],
        "judge_options": {
            "true": ["对", "正确", "√", "T"],
            "false": ["错", "错误", "×", "F"],
        },
        "next_button": ["下一题", "下一页", "下题"],
        "submit_button": ["提交"],
        "ignore_blocks": ["学习通", "正确答案", "我的答案", "解析", "得分",
                          "收藏", "答题卡", "全屏", "暂时保存"],
        "blank_click_dx": 100,
        "blank_region_width": 500,
        "short_answer_click": [260, 145],
        "toolbar_indent": 25,
        "focus_click_x": 70,
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


def build_config(overrides: dict | None = None) -> dict:
    """以完整 schema 为底深叠加覆盖项(测试/工具构造配置用,不做合法性校验)"""
    return _merge(DEFAULT_CONFIG, overrides or {})


# ---------- 合法性校验 ----------

_ENUMS = {
    ("window", "capture_method"): {"printwindow", "bitblt"},
    ("action", "answer_mode"): {"long_screenshot", "per_question"},
    ("ui", "theme"): {"dark", "light", "auto"},
    ("web", "default_browser"): {"", "edge", "chrome"},
}
_PORTS = {("web", "cdp_port"), ("web", "ext_bridge_port")}
# [最小值, 最大值] 两元素数字区间
_RANGE_KEYS = {
    ("action", "click_delay"), ("action", "option_interval"),
    ("action", "next_delay"), ("web", "q_delay"), ("web", "opt_delay"),
}
_THRESHOLD_KEYS = {("ocr", "confidence_threshold"), ("ocr", "retry_threshold")}
_BOOL_KEYS = {
    ("action", "dry_run"), ("web", "launch_browser"), ("log", "save_to_file"),
}
# 非负/正数值键(包含 int 与 float,排除 bool)
_NON_NEGATIVE_NUM = {
    ("action", "page_wait"), ("action", "scan_settle"),
    ("action", "fine_scroll_wait"), ("action", "verify_wait"),
}
_POSITIVE_NUM = {("llm", "timeout"), ("web", "wait_page_timeout")}
_NON_NEGATIVE_INT = {
    ("llm", "max_retries"), ("action", "fine_scroll_steps"),
}
_POSITIVE_INT = {("llm", "concurrency"), ("action", "max_scan_frames")}

_ROI_INT_KEYS = [
    "answer_card_width", "option_indent", "option_max_offset_x",
    "option_line_gap", "option_row_gap", "stem_option_gap", "bottom_margin",
    "blank_click_dx", "blank_region_width", "toolbar_indent", "focus_click_x",
]
_ROI_INT_LIST_KEYS = ["content_region", "short_answer_click"]
_ROI_STR_LIST_KEYS = [
    "option_labels", "next_button", "submit_button", "ignore_blocks",
]


def _is_num(v) -> bool:
    return isinstance(v, numbers.Number) and not isinstance(v, bool)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _fail(msg: str):
    raise ValueError(f"配置非法: {msg}")


def validate_config(data: dict):
    """校验已合并的完整配置树;非法项抛 ValueError 并指明节.键与原因。"""
    if not isinstance(data, dict):
        _fail("配置根节点必须是映射表")

    for section, schema in DEFAULT_CONFIG.items():
        if section not in data:
            _fail(f"缺少配置节 [{section}]")
        if not isinstance(data[section], dict):
            _fail(f"配置节 [{section}] 必须是映射表")
        missing = [k for k in schema if k not in data[section]]
        if missing:
            _fail(f"配置节 [{section}] 缺少键: {', '.join(missing)}")

    def need(section, key, pred, what):
        v = data[section][key]
        if not pred(v):
            _fail(f"{section}.{key} 必须是{what},实际为 {v!r}")

    for (section, key), allowed in _ENUMS.items():
        v = data[section][key]
        if v not in allowed:
            _fail(f"{section}.{key} 必须是 {sorted(allowed)} 之一,实际为 {v!r}")

    for section, key in _PORTS:
        v = data[section][key]
        if not _is_int(v) or not (1024 <= v <= 65535):
            _fail(f"{section}.{key} 必须是 1024-65535 的整数端口,实际为 {v!r}")

    for section, key in _RANGE_KEYS:
        v = data[section][key]
        if not (isinstance(v, list) and len(v) == 2
                and all(_is_num(x) for x in v) and v[0] <= v[1]):
            _fail(f"{section}.{key} 必须是 [最小值, 最大值] 且最小值≤最大值,"
                  f"实际为 {v!r}")

    for section, key in _THRESHOLD_KEYS:
        v = data[section][key]
        if not _is_num(v) or not (0 < v <= 1):
            _fail(f"{section}.{key} 必须是 (0,1] 之间的数值,实际为 {v!r}")

    for section, key in _BOOL_KEYS:
        need(section, key, lambda v: isinstance(v, bool), "布尔值 true/false")

    for section, key in _NON_NEGATIVE_NUM:
        need(section, key, lambda v: _is_num(v) and v >= 0, "非负数")
    for section, key in _POSITIVE_NUM:
        need(section, key, lambda v: _is_num(v) and v > 0, "正数")
    for section, key in _NON_NEGATIVE_INT:
        need(section, key, _is_int, "非负整数")
        if data[section][key] < 0:
            _fail(f"{section}.{key} 必须是非负整数,实际为 {data[section][key]!r}")
    for section, key in _POSITIVE_INT:
        need(section, key, _is_int, "正整数")
        if data[section][key] < 1:
            _fail(f"{section}.{key} 必须是正整数,实际为 {data[section][key]!r}")

    # 字符串 / 字符串列表
    for key in ("base_url", "api_key", "model"):
        need("llm", key, lambda v: isinstance(v, str), "字符串")
    temperature = data["llm"]["temperature"]
    if not _is_num(temperature) or not (0 <= temperature <= 2):
        _fail(f"llm.temperature 必须是 [0,2] 之间的数值,实际为 {temperature!r}")
    step_ratio = data["action"]["scan_step_ratio"]
    if not _is_num(step_ratio) or not (0 < step_ratio <= 1):
        _fail(f"action.scan_step_ratio 必须是 (0,1] 之间的比例,实际为 {step_ratio!r}")
    need("window", "title_keywords",
         lambda v: isinstance(v, list) and v and all(isinstance(x, str) for x in v),
         "非空字符串列表")
    need("web", "url_keywords",
         lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
         "字符串列表")
    need("log", "dir", lambda v: isinstance(v, str) and v, "非空字符串")

    # roi 几何标定
    roi = data["roi"]
    if set(roi) != set(DEFAULT_CONFIG["roi"]):
        extra = set(roi) - set(DEFAULT_CONFIG["roi"])
        miss = set(DEFAULT_CONFIG["roi"]) - set(roi)
        _fail(f"roi 键集合不符(多余: {sorted(extra)}, 缺失: {sorted(miss)})")
    for key in _ROI_INT_KEYS:
        if not _is_int(roi[key]):
            _fail(f"roi.{key} 必须是整数,实际为 {roi[key]!r}")
    for key in _ROI_INT_LIST_KEYS:
        v = roi[key]
        if not (isinstance(v, list) and v and all(_is_int(x) for x in v)):
            _fail(f"roi.{key} 必须是非空整数列表,实际为 {v!r}")
    for key in _ROI_STR_LIST_KEYS:
        v = roi[key]
        if not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
            _fail(f"roi.{key} 必须是非空字符串列表,实际为 {v!r}")
    for side in ("true", "false"):
        v = roi["judge_options"][side]
        if not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
            _fail(f"roi.judge_options.{side} 必须是非空字符串列表,实际为 {v!r}")


def _warn_unknown(data: dict):
    """对 schema 之外的节/键告警(帮助发现拼写错误;值仍保留不删)。"""
    for section, val in data.items():
        if section in _RESERVED_SECTIONS:
            continue
        if section not in DEFAULT_CONFIG:
            logger.warning("配置文件含未知节 [%s],不被任何代码读取,请检查拼写", section)
            continue
        if isinstance(val, dict):
            for key in val:
                if key not in DEFAULT_CONFIG[section]:
                    logger.warning("未知配置项 %s.%s,不被任何代码读取,请检查拼写",
                                   section, key)


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
        _warn_unknown(self._data)
        validate_config(self._data)

    def save(self):
        validate_config(self._data)
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
