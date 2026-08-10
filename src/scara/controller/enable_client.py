"""调用 D:\\SNRobotLab\\scara_enable.exe（直调 RobotSDK.dll）。

与 do_client / scara_do 同模式：短生命周期进程，不经 snrobot serve。
调用前须释放控制器连接（ScaraController 会临时断开 serve）。

状态字段对应官方 UI：
  Read_PowerOn  → enable  (ENABLE_ON/OFF)
  Read_Stop     → estop   (ESTOP_ON/OFF)
  Read_Warning  → warn    (ALARM_ON/OFF)
  ReadCurMode   → mode
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_DEFAULT_SDK = Path(os.environ.get("SNROBOTLAB_DIR", r"D:\SNRobotLab"))

_MODE_FROM_CODE = {1: "T1", 2: "T2", 3: "Execute", 4: "Remote"}


def sdk_dir() -> Path:
    return Path(os.environ.get("SNROBOTLAB_DIR", str(_DEFAULT_SDK)))


def exe_path() -> Path:
    env = os.environ.get("SCARA_ENABLE_EXE")
    if env:
        return Path(env)
    return sdk_dir() / "scara_enable.exe"


def _active_ip_port() -> Tuple[str, int]:
    cfg = sdk_dir() / "WorkStationConfig.json"
    rows = json.loads(cfg.read_text(encoding="utf-8"))
    for row in rows:
        if row.get("Active"):
            return str(row["IP"]), int(row["Port"])
    raise RuntimeError(f"{cfg} 没有 Active 工位")


def parse_status(text: str) -> Dict[str, Any]:
    """从 scara_enable 输出解析安全状态（取文中最后一次出现）。"""
    enable = 0
    estop = False
    warn = 0
    mode = "?"
    for line in text.splitlines():
        if "ENABLE_ON" in line:
            enable = 1
        elif "ENABLE_OFF" in line:
            enable = 0
        if "ESTOP_ON" in line:
            estop = True
        elif "ESTOP_OFF" in line:
            estop = False
        if "ALARM_ON" in line:
            warn = 1
        elif "ALARM_OFF" in line:
            warn = 0
        if "ReadCurMode" in line:
            m = re.search(r"mode\s*=\s*(\d+)", line)
            if m:
                mode = _MODE_FROM_CODE.get(int(m.group(1)), f"?{m.group(1)}")
            else:
                for name in ("T1", "T2", "Execute", "Remote"):
                    if f"({name})" in line:
                        mode = name
                        break
    return {
        "enable": enable,
        "estop": bool(estop),
        "warn": int(warn),
        "mode": mode,
    }


def run_cmd(
    *args: str,
    timeout_s: float = 25.0,
    ip: Optional[str] = None,
    port: Optional[int] = None,
) -> Tuple[bool, str]:
    """跑 scara_enable.exe；成功返回 (True, 输出)，失败 (False, 输出/错误)。"""
    exe = exe_path()
    if not exe.is_file():
        return False, (
            f"找不到 {exe}。请先运行 tools\\scara_enable\\build.bat "
            f"（会编译并复制到 {sdk_dir()}）"
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
    """读使能/急停/报警/模式。DLL: Read_PowerOn / Read_Stop / Read_Warning / ReadCurMode。"""
    return run_cmd("status", **kw)


def enable(**kw) -> Tuple[bool, str]:
    """使能：Send_PowerOn → Reset_PowerOn 脉冲。"""
    return run_cmd("enable", **kw)


def disable(**kw) -> Tuple[bool, str]:
    """去使能：SetMode 来回切，不触发急停。"""
    return run_cmd("disable", **kw)


def set_mode(mode: int, **kw) -> Tuple[bool, str]:
    """切模式 1=T1 2=T2 3=执行 4=远程。"""
    return run_cmd("set_mode", str(int(mode)), **kw)


def estop(**kw) -> Tuple[bool, str]:
    """急停：Send_Stop。"""
    return run_cmd("estop", **kw)


def clear_alarm(**kw) -> Tuple[bool, str]:
    """清报警：Reset_Stop 等到 OFF → Send_Reset/Reset_Reset。"""
    return run_cmd("clear_alarm", **kw)
