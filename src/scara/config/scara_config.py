"""
SCARA 机械臂模块 —— 配置模型

集中管理与新松 SA4A-4/0.40 通信及运动相关的默认参数。
控制不走裸协议，而是调用位于 SNRobotLab 目录下的命令行桥 ``snrobot.exe``
（该目录含许可证 SiaSunRobot.lic，exe 必须在此目录运行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:  # Python 3.11+ 自带 tomllib
    import tomllib as _toml
except ModuleNotFoundError:  # 3.10 回退到 tomli
    import tomli as _toml  # type: ignore


# 默认 snrobot.exe 所在目录（含 RobotCommunication SDK DLL + 许可证 SiaSunRobot.lic，exe 必须在此运行）
# 本机 SNRobotLab 装在 C 盘（无 D 盘）；snrobot.exe 桥已复制到此目录与 SDK/许可证同放。
DEFAULT_EXE_DIR = r"D:\SNRobotLab"
DEFAULT_EXE_NAME = "snrobot.exe"


@dataclass
class ScaraConfig:
    """SCARA 模块配置。"""

    # —— 连接 ——
    exe_dir: str = DEFAULT_EXE_DIR            # snrobot.exe 所在目录（工作目录）
    exe_name: str = DEFAULT_EXE_NAME
    controller_ip: str = "192.168.1.100"     # 仅用于界面显示（IP 固化在 exe 内）
    controller_port: int = 20002
    command_timeout_s: float = 8.0           # 单条命令超时
    poll_interval_ms: int = 300              # 状态轮询间隔(越小越实时)

    # —— 运动 ——
    default_speed_percent: int = 20
    min_speed_percent: int = 1
    max_speed_percent: int = 100
    default_joint_step_deg: float = 1.0      # 关节点动步长（度；J3=mm）
    default_cart_step_mm: float = 5.0        # 笛卡尔点动步长（mm）
    move_hold_s: int = 4                     # 单步运动后保持/观察秒数（命令内部）

    # —— 安全 ——
    require_enable_before_motion: bool = True
    disable_on_disconnect: bool = False
    allow_t2_mode: bool = False              # 是否允许切到 T2（全速）

    # —— IO ——
    di_preview_count: int = 32               # 界面预览的 DI 通道数
    do_preview_count: int = 32               # 界面预览的 DO 通道数

    @property
    def exe_path(self) -> str:
        return str(Path(self.exe_dir) / self.exe_name)

    def clamp_speed(self, v: int) -> int:
        return max(self.min_speed_percent, min(self.max_speed_percent, int(v)))


def load_scara_config(path: Optional[str] = None) -> ScaraConfig:
    """从 ``scara_config.toml`` 加载配置；文件不存在时返回默认配置。"""
    cfg = ScaraConfig()
    toml_path = Path(path) if path else Path("scara_config.toml")
    if not toml_path.exists():
        return cfg
    try:
        with open(toml_path, "rb") as f:
            data = _toml.load(f)
        conn = data.get("connection", {})
        cfg.exe_dir = conn.get("exe_dir", cfg.exe_dir)
        cfg.exe_name = conn.get("exe_name", cfg.exe_name)
        cfg.controller_ip = conn.get("controller_ip", cfg.controller_ip)
        cfg.controller_port = int(conn.get("controller_port", cfg.controller_port))
        cfg.command_timeout_s = float(conn.get("command_timeout_s", cfg.command_timeout_s))
        cfg.poll_interval_ms = int(conn.get("poll_interval_ms", cfg.poll_interval_ms))
        mot = data.get("motion", {})
        cfg.default_speed_percent = int(mot.get("default_speed_percent", cfg.default_speed_percent))
        cfg.default_joint_step_deg = float(mot.get("default_joint_step_deg", cfg.default_joint_step_deg))
        cfg.default_cart_step_mm = float(mot.get("default_cart_step_mm", cfg.default_cart_step_mm))
        cfg.move_hold_s = int(mot.get("move_hold_s", cfg.move_hold_s))
        saf = data.get("safety", {})
        cfg.require_enable_before_motion = bool(saf.get("require_enable_before_motion", cfg.require_enable_before_motion))
        cfg.disable_on_disconnect = bool(saf.get("disable_on_disconnect", cfg.disable_on_disconnect))
        cfg.allow_t2_mode = bool(saf.get("allow_t2_mode", cfg.allow_t2_mode))
    except Exception:
        # 配置损坏时回退默认，不阻断模块加载
        return ScaraConfig()
    return cfg
