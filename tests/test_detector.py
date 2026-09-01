# -*- coding: utf-8 -*-
"""环境检测模块测试:验证探测函数行为与返回结构(不依赖具体安装)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.env.detector import (
    AppInfo, _app_paths_lookup, _find_file, detect_all, detect_ides,
)


def test_app_paths_lookup_missing():
    """注册表查不存在的 exe 应返回 None 而非抛异常"""
    assert _app_paths_lookup("definitely_not_exist_9999.exe") is None


def test_find_file_missing():
    assert _find_file(r"C:\definitely\not\exist\path\x.exe") is None


def test_find_file_glob():
    """通配符路径:项目根下必能匹配 main.py"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hit = _find_file(os.path.join(root, "*.py").replace("\\", r"\\"))
    # 上面路径不含通配符转义问题则直接验证
    assert hit is None or os.path.isfile(hit)
    # 直接用通配符
    hit2 = _find_file(root + os.sep + "ma?n.py")
    assert hit2 and os.path.isfile(hit2)


def test_detect_all_shape():
    """detect_all 返回结构完整可序列化"""
    result = detect_all()
    assert set(result) == {"ides", "agents", "pythons"}
    for section in result.values():
        assert isinstance(section, list)
        for it in section:
            assert set(it) == {"name", "kind", "path", "source", "cli"}
            assert it["kind"] in ("ide", "agent", "python")
            assert it["source"] in ("which", "registry", "path", "python")


def test_appinfo_dataclass():
    info = AppInfo("X", "ide", "C:/x.exe", "path", "x")
    d = info.__dict__
    assert d["name"] == "X" and d["cli"] == "x"


def test_detect_ides_no_crash():
    """任何机器上运行都不应崩溃"""
    assert isinstance(detect_ides(), list)
