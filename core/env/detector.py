# -*- coding: utf-8 -*-
"""开发环境检测:扫描本机已安装的 IDE / AI 编程工具 / Python。

检测渠道(按可靠度):
1. PATH 上的 CLI(shutil.which) —— 可直接命令行调用
2. 注册表 App Paths(HKLM + HKCU) —— 开始菜单/运行对话框注册的程序
3. 常见安装路径 —— 免安装版/未注册的兜底

供后续"自动做编程题"模块选择执行载体(IDLE/VS Code/Cursor 等)。
"""
import os
import shutil
import winreg
from dataclasses import dataclass, asdict

from core.log import get_logger

logger = get_logger("env.detector")

# ---- 检测目标定义 ----
# exe: which/App Paths/路径探测的可执行文件名(小写)
# cli: PATH 上的命令行工具名(None 表示无 CLI)
# paths: 常见安装路径(支持环境变量展开)

IDES = {
    "VS Code": {
        "exe": "code.exe",
        "cli": "code",
        "paths": [
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
        ],
    },
    "Cursor": {
        "exe": "cursor.exe",
        "cli": "cursor",
        "paths": [
            r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe",
            r"%LOCALAPPDATA%\Programs\Cursor\Cursor.exe",
        ],
    },
    "PyCharm": {
        "exe": "pycharm64.exe",
        "cli": "pycharm",
        "paths": [
            r"%ProgramFiles%\JetBrains\PyCharm*\bin\pycharm64.exe",
            r"%LOCALAPPDATA%\JetBrains\Toolbox\apps\PyCharm-P\ch-0\*\bin\pycharm64.exe",
        ],
    },
    "IDLE": {  # Python 自带,检测到 Python 后另行探测
        "exe": "idle.exe",
        "cli": "idle",
        "paths": [],
    },
    "Sublime Text": {
        "exe": "sublime_text.exe",
        "cli": "subl",
        "paths": [
            r"%ProgramFiles%\Sublime Text\sublime_text.exe",
            r"%LOCALAPPDATA%\Programs\Sublime Text\sublime_text.exe",
        ],
    },
    "Windsurf": {
        "exe": "windsurf.exe",
        "cli": "windsurf",
        "paths": [
            r"%LOCALAPPDATA%\Programs\Windsurf\Windsurf.exe",
        ],
    },
    "Trae": {
        "exe": "trae.exe",
        "cli": "trae",
        "paths": [
            r"%LOCALAPPDATA%\Programs\Trae\Trae.exe",
        ],
    },
}

# AI 编程 agent / CLI(仅 PATH 探测,无 GUI)
AGENTS = {
    "Claude Code": ["claude"],
    "Gemini CLI": ["gemini"],
    "GitHub Copilot CLI": ["github-copilot-cli", "copilot"],
    "Aider": ["aider"],
    "OpenCode": ["opencode"],
    "Cline": ["cline"],
    "OpenHands": ["openhands"],
    "AMP": ["amp"],
}


@dataclass
class AppInfo:
    name: str          # 展示名,如 "VS Code"
    kind: str          # ide / agent / python
    path: str          # 可执行文件或 CLI 完整路径
    source: str        # which / registry / path / python
    cli: str = ""      # 可调用的 CLI 命令(空表示无)


def _expand(p: str) -> str:
    return os.path.expandvars(p)


def _glob_first(pattern: str) -> str | None:
    import glob
    hits = glob.glob(_expand(pattern))
    return hits[0] if hits else None


def _app_paths_lookup(exe: str) -> str | None:
    """注册表 App Paths 查找(HKLM 优先,失败查 HKCU)"""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(
                hive,
                rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}")
        except OSError:
            continue
        try:
            val, _ = winreg.QueryValueEx(key, None)
            if val and os.path.isfile(os.path.expandvars(val)):
                return val
        except OSError:
            pass
        finally:
            winreg.CloseKey(key)
    return None


def _find_file(p: str) -> str | None:
    """路径存在性检查(支持通配符)"""
    if "*" in p or "?" in p:
        return _glob_first(p)
    p = _expand(p)
    return p if os.path.isfile(p) else None


def _detect_from_spec(name: str, spec: dict, kind: str) -> AppInfo | None:
    """按 name/exe/cli/paths 定义探测一个应用,命中返回 AppInfo"""
    exe, cli, paths = spec["exe"], spec.get("cli"), spec.get("paths", [])

    # 1) PATH 上的 CLI
    if cli:
        which = shutil.which(cli)
        if which:
            return AppInfo(name, kind, which, "which", cli)
    # 2) 注册表 App Paths
    hit = _app_paths_lookup(exe)
    if hit:
        return AppInfo(name, kind, hit, "registry", cli or "")
    # 3) 常见安装路径
    for p in paths:
        hit = _find_file(p)
        if hit:
            return AppInfo(name, kind, hit, "path", cli or "")
    return None


def detect_ides() -> list[AppInfo]:
    """检测已安装的 IDE"""
    results = []
    for name, spec in IDES.items():
        info = _detect_from_spec(name, spec, "ide")
        if info:
            results.append(info)
    return results


def detect_agents() -> list[AppInfo]:
    """检测 PATH 上的 AI 编程 agent / CLI"""
    results = []
    for name, cmds in AGENTS.items():
        for cmd in cmds:
            which = shutil.which(cmd)
            if which:
                results.append(AppInfo(name, "agent", which, "which", cmd))
                break
    return results


def detect_pythons() -> list[AppInfo]:
    """检测 Python 解释器与自带的 IDLE"""
    results = []
    # PATH 上的 python
    for cmd in ("python", "python3", "py"):
        which = shutil.which(cmd)
        if which:
            results.append(AppInfo(f"Python ({cmd})", "python", which, "which", cmd))
            break
    # venv 意味着宿主 Python 也在(venv 由本项目 .venv 推断)
    venv_py = _expand(r"%VIRTUAL_ENV%\Scripts\python.exe")
    if venv_py and os.path.isfile(venv_py):
        results.append(AppInfo("Python (venv)", "python", venv_py, "path", "python"))
    # IDLE:从 py -0 列举太慢,直接探测常见安装位
    idle = (_find_file(r"%LOCALAPPDATA%\Programs\Python\Python*\Lib\idlelib\idle.pyw")
            or _find_file(r"%ProgramFiles%\Python*\Lib\idlelib\idle.pyw")
            or shutil.which("idle"))
    if idle:
        results.append(AppInfo("IDLE", "python", idle, "path", "idle"))
    return results


def detect_all() -> dict:
    """检测全部环境,返回可序列化字典"""
    ides = detect_ides()
    agents = detect_agents()
    pythons = detect_pythons()
    # IDLE 可能同时出现在 IDE 与 Python 检测结果中,去重(保留 IDE 分类)
    ide_names = {i.name for i in ides}
    pythons = [p for p in pythons if p.name != "IDLE" or "IDLE" not in ide_names]
    logger.info(f"环境检测: IDE {len(ides)} 个 / Agent {len(agents)} 个 / "
                f"Python {len(pythons)} 个")
    return {
        "ides": [asdict(i) for i in ides],
        "agents": [asdict(a) for a in agents],
        "pythons": [asdict(p) for p in pythons],
    }


if __name__ == "__main__":
    # 独立运行:打印检测结果
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from core.log import setup_logging
    setup_logging()
    for section, items in detect_all().items():
        print(f"\n[{section}]")
        if not items:
            print("  (未检测到)")
        for it in items:
            src = f"({it['source']})"
            print(f"  {it['name']:<20} {src:<12} {it['path']}")
