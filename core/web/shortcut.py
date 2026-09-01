# -*- coding: utf-8 -*-
"""
浏览器快捷方式管理:为 Edge/Chrome 的快捷方式一键追加 --remote-debugging-port 参数。

浏览器只在启动时读该参数才会开调试端口(运行中无法事后开启),
改快捷方式 = 用户每次正常打开浏览器都自带端口,程序零重启直连。
"""
import os
import subprocess
from pathlib import Path

from core.log import get_logger

logger = get_logger("web.shortcut")

# 快捷方式目标对应的浏览器(用于识别该快捷方式是否是 Edge/Chrome)
BROWSER_NAMES = ("msedge.exe", "chrome.exe")

# 默认浏览器标识 → 目标 exe 名
BROWSER_EXE = {"edge": "msedge.exe", "chrome": "chrome.exe"}


def _powershell(args: list[str]) -> tuple[int, str]:
    """执行 PowerShell 命令,返回 (returncode, 输出)"""
    cmd = ["powershell", "-NoProfile", "-Command"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def find_shortcuts(browser: str = "") -> list[dict]:
    """扫描桌面/快速启动/开始菜单中的 Edge/Chrome 快捷方式。

    :param browser: "" = 全部; "edge" / "chrome" = 只扫该浏览器
    返回 [{path, target, has_port}]:
    - has_port: 目标是否已含 --remote-debugging-port
    """
    dirs = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            start_menu = Path(base) / "Microsoft/Windows/Start Menu/Programs"
            if start_menu.is_dir():
                dirs.append(start_menu)
    quick_launch = Path(os.environ.get("APPDATA", "")) / \
        "Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar"
    if quick_launch.is_dir():
        dirs.append(quick_launch)
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        dirs.append(desktop)
    # 公共桌面(所有用户桌面)
    pub_desktop = Path(os.environ.get("PUBLIC", "")) / "Desktop"
    if pub_desktop.is_dir():
        dirs.append(pub_desktop)

    script = r"""
$sh = New-Object -ComObject WScript.Shell
Get-ChildItem -Path @('%s') -Filter *.lnk -Recurse -ErrorAction SilentlyContinue |
  ForEach-Object {
    $lnk = $sh.CreateShortcut($_.FullName)
    [PSCustomObject]@{ Path = $_.FullName; Target = $lnk.TargetPath }
  } | ConvertTo-Json
""" % ("','".join(str(d) for d in dirs))

    code, out = _powershell([script])
    if code != 0 or not out.strip():
        return []

    import json
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]

    result = []
    names = (BROWSER_EXE[browser],) if browser in BROWSER_EXE else BROWSER_NAMES
    for item in data:
        target = (item.get("Target") or "").strip().lower()
        if not any(name in target for name in names):
            continue
        result.append({
            "path": item["Path"],
            "target": item.get("Target") or "",
            "has_port": "remote-debugging-port" in target,
        })
    return result


def add_port_to_shortcut(lnk_path: str, port: int = 9222) -> tuple[bool, str]:
    """为单个快捷方式追加调试端口参数(已有则跳过)。返回 (成功?, 说明)"""
    script = r"""
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut('%s')
if ($lnk.Arguments -match 'remote-debugging-port') {
    'ALREADY'
} else {
    $lnk.Arguments = ($lnk.Arguments + ' --remote-debugging-port=%d').Trim()
    $lnk.Save()
    'OK'
}
""" % (lnk_path.replace("'", "''"), port)

    code, out = _powershell([script])
    out = out.strip()
    if code != 0:
        return False, f"修改失败: {out[:200]}"
    if out == "ALREADY":
        return True, "该快捷方式已带调试端口,无需修改"
    return True, f"已追加 --remote-debugging-port={port}"


def add_port_to_all(port: int = 9222, browser: str = "") -> list[dict]:
    """为找到的快捷方式追加端口。
    :param browser: "" = 全部浏览器; "edge" / "chrome" = 只处理该浏览器
    返回每个快捷方式的处理结果 [{path, ok, msg}]。"""
    results = []
    for sc in find_shortcuts(browser):
        ok, msg = add_port_to_shortcut(sc["path"], port)
        logger.info(f"[快捷方式] {sc['path']}: {msg}")
        results.append({"path": sc["path"], "ok": ok, "msg": msg})
    return results
