"""调用 D:\\SNRobotLab\\scara_do.exe 写用户输出 UO（方案 C）。

scara_do 内部：WriteUOEnable(通道,0) 保持用户模式 U，再 WriteUO(通道, 电平)。

电平约定（与现场接线有关）：
  默认：开泵 = UO 写 1，停泵 = UO 写 0。
  若你的泵是「0 开、1 关」，把下面 UO_LEVEL_ON / UO_LEVEL_OFF 对调即可。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

_DEFAULT_SDK = Path(os.environ.get("SNROBOTLAB_DIR", r"D:\SNRobotLab"))

# 写到控制器的 UO 电平：多数泵是 1=开、0=关；反接逻辑时把两值对调
UO_LEVEL_ON = 1
UO_LEVEL_OFF = 0


def sdk_dir() -> Path:
    return Path(os.environ.get("SNROBOTLAB_DIR", str(_DEFAULT_SDK)))


def exe_path() -> Path:
    env = os.environ.get("SCARA_DO_EXE")
    if env:
        return Path(env)
    return sdk_dir() / "scara_do.exe"


def _active_ip_port() -> Tuple[str, int]:
    cfg = sdk_dir() / "WorkStationConfig.json"
    rows = json.loads(cfg.read_text(encoding="utf-8"))
    for row in rows:
        if row.get("Active"):
            return str(row["IP"]), int(row["Port"])
    raise RuntimeError(f"{cfg} 没有 Active 工位")


def set_uo(
    channel: int,
    on: bool,
    *,
    timeout_s: float = 15.0,
    ip: Optional[str] = None,
    port: Optional[int] = None,
) -> Tuple[bool, str]:
    """写用户输出 UO[channel]。on=True 表示开泵，False 表示停泵（见 UO_LEVEL_ON/OFF）。"""
    exe = exe_path()
    if not exe.is_file():
        return False, (
            f"找不到 {exe}。请先运行 tools\\scara_do\\build.bat "
            f"（会编译并复制到 {sdk_dir()}）"
        )

    if ip is None or port is None:
        ip, port = _active_ip_port()

    level = UO_LEVEL_ON if on else UO_LEVEL_OFF
    cmd = [
        str(exe), "set", str(int(channel)), str(level),
        "--ip", str(ip), "--port", str(int(port)),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(exe.parent),
        )
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT: scara_do 超过 {timeout_s:.0f}s"
    except OSError as exc:
        return False, f"启动失败: {exc}"

    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return False, out or f"exit={proc.returncode}"
    return True, out
