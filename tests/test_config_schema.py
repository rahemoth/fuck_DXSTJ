# -*- coding: utf-8 -*-
"""统一配置 schema 测试:
- DEFAULT_CONFIG 自身合法且键完整;
- config.example.yaml 与 DEFAULT_CONFIG 叶子键严格一致(防漂移);
- 非法配置在加载校验层显式报错;
- 从旧 roi.json 迁入的几何值逐键保真。
"""
from pathlib import Path

import pytest
import yaml

from core.config import (
    DEFAULT_CONFIG, build_config, validate_config, _merge,
)

ROOT = Path(__file__).parent.parent


def _leaf_paths(d: dict, prefix: str = "") -> set[str]:
    """返回所有非字典叶子的点分路径(列表/标量均视为叶子)"""
    paths = set()
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            paths |= _leaf_paths(v, p)
        else:
            paths.add(p)
    return paths


# ---------- 默认 schema 自身 ----------

def test_default_config_validates():
    validate_config(DEFAULT_CONFIG)


def test_schema_sections_and_roi_keys():
    assert set(DEFAULT_CONFIG) == {
        "window", "llm", "action", "ocr", "web", "ui", "log", "roi"}
    assert len(DEFAULT_CONFIG["roi"]) == 18


def test_roi_values_migrated_from_old_roi_json():
    """roi.json 删除后,其原值必须逐字保留在 schema 中"""
    roi = DEFAULT_CONFIG["roi"]
    assert roi["content_region"] == [100, 0, 1000, 99999]
    assert roi["answer_card_width"] == 190
    assert roi["option_indent"] == 30
    assert roi["option_max_offset_x"] == 250
    assert roi["option_line_gap"] == 35
    assert roi["option_row_gap"] == 49
    assert roi["stem_option_gap"] == 110
    assert roi["bottom_margin"] == 60
    assert roi["option_labels"] == ["A", "B", "C", "D", "E", "F"]
    assert roi["judge_options"] == {
        "true": ["对", "正确", "√", "T"],
        "false": ["错", "错误", "×", "F"]}
    assert roi["next_button"] == ["下一题", "下一页", "下题"]
    assert roi["submit_button"] == ["提交"]
    assert roi["ignore_blocks"] == [
        "学习通", "正确答案", "我的答案", "解析", "得分",
        "收藏", "答题卡", "全屏", "暂时保存"]
    assert roi["blank_click_dx"] == 100
    assert roi["blank_region_width"] == 500
    assert roi["short_answer_click"] == [260, 145]
    assert roi["toolbar_indent"] == 25
    assert roi["focus_click_x"] == 70


# ---------- example 模板与 schema 一致性 ----------

def test_example_matches_schema_leaf_keys():
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert _leaf_paths(data) == _leaf_paths(DEFAULT_CONFIG)


def test_example_itself_validates():
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    validate_config(_merge(DEFAULT_CONFIG, data))


# ---------- build_config ----------

def test_build_config_deep_overrides_and_completes():
    cfg = build_config({"action": {"page_wait": 0}})
    assert cfg["action"]["page_wait"] == 0            # 覆盖生效
    assert cfg["action"]["verify_wait"] == 0.6        # 同节其余键补齐
    assert cfg["roi"]["focus_click_x"] == 70          # 其他节补齐


def test_build_config_independent_copies():
    a = build_config()
    a["action"]["page_wait"] = 99
    b = build_config()
    assert b["action"]["page_wait"] == 1.0


# ---------- 非法值必须显式报错 ----------

@pytest.mark.parametrize("section,key,bad", [
    ("window", "capture_method", "dxgi"),
    ("action", "answer_mode", "turbo"),
    ("ui", "theme", "blue"),
    ("web", "default_browser", "firefox"),
    ("web", "cdp_port", 80),
    ("web", "ext_bridge_port", 70000),
    ("action", "click_delay", [1.8, 0.8]),
    ("action", "option_interval", [0.3]),
    ("ocr", "confidence_threshold", 1.5),
    ("ocr", "retry_threshold", 0),
    ("llm", "timeout", 0),
    ("llm", "concurrency", 0),
    ("llm", "max_retries", -1),
    ("action", "max_scan_frames", 0),
    ("action", "scan_step_ratio", 1.2),
    ("action", "dry_run", "yes"),
    ("llm", "temperature", 3),
])
def test_invalid_values_raise(section, key, bad):
    cfg = build_config({section: {key: bad}})
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_missing_section_and_key_raise():
    cfg = build_config()
    del cfg["roi"]
    with pytest.raises(ValueError):
        validate_config(cfg)

    cfg = build_config()
    del cfg["action"]["page_wait"]
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_roi_key_set_must_match_exactly():
    cfg = build_config({"roi": {"extra_junk": 1}})
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_unknown_keys_do_not_break_validation():
    """未知键(如旧版死键)只告警不报错,schema 键仍可用"""
    cfg = build_config({"action": {"scroll_clicks": 2}, "course": {"mode": "next"}})
    validate_config(cfg)
    assert cfg["action"]["scroll_clicks"] == 2
    assert cfg["course"]["mode"] == "next"
