"""调用 scara_enable.exe（直调 RobotSDK.dll）。

独立测试脚本：路径与 main.py 相同，读仓库根目录 local_config.toml。
前提：关掉官方 SCARA GUI 和本程序 snrobot serve（会抢连接）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from scara.config.scara_config import resolve_snrobotlab_dir  # noqa: E402


def sdk_dir() -> Path:
    env = os.environ.get("SNROBOTLAB_DIR", "").strip()
    if env:
        return Path(env)
    return resolve_snrobotlab_dir()


def exe_path() -> Path:
    env = os.environ.get("SCARA_ENABLE_EXE", "").strip()
    if env:
        return Path(env)
    return sdk_dir() / "scara_enable.exe"


def _active_ip_port() -> Tuple[str, int]:
    from scara.config.scara_config import load_scara_config

    cfg = sdk_dir() / "WorkStationConfig.json"
    if cfg.is_file():
        rows = json.loads(cfg.read_text(encoding="utf-8"))
        for row in rows:
            if row.get("Active"):
                return str(row["IP"]), int(row["Port"])
        raise RuntimeError(f"{cfg} 没有 Active 工位")
    sc = load_scara_config()
    return str(sc.controller_ip), int(sc.controller_port)


def run_cmd(
    *args: str,
    timeout_s: float = 20.0,
    ip: Optional[str] = None,
    port: Optional[int] = None,
) -> Tuple[bool, str]:
    """跑 scara_enable.exe；成功返回 (True, 输出)，失败 (False, 输出/错误)。"""
    try:
        exe = exe_path()
    except FileNotFoundError as exc:
        return False, str(exc)
    if not exe.is_file():
        return False, (
            f"找不到 {exe}。请把 tools\\scara_enable\\scara_enable.exe "
            f"复制到 {sdk_dir()}（见 local_config.toml / 路径硬编码清单.md）"
        )
    if ip is None or port is None:
        ip, port = _active_ip_port()
    cmd = [str(exe), *args, "--ip", str(ip), "--port", str(int(port))]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_s,
            cwd=str(exe.parent),
        )
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT: scara_enable 超过 {timeout_s:.0f}s"
    except OSError as exc:
        return False, f"启动失败: {exc}"
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    out = raw.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return False, out or f"exit={proc.returncode}"
    return True, out


def status(**kw) -> Tuple[bool, str]:
    return run_cmd("status", **kw)


def enable(**kw) -> Tuple[bool, str]:
    return run_cmd("enable", **kw)


def disable(**kw) -> Tuple[bool, str]:
    return run_cmd("disable", **kw)


def set_mode(mode: int, **kw) -> Tuple[bool, str]:
    return run_cmd("set_mode", str(int(mode)), **kw)


def estop(**kw) -> Tuple[bool, str]:
    return run_cmd("estop", **kw)


def clear_alarm(**kw) -> Tuple[bool, str]:
    return run_cmd("clear_alarm", **kw)
