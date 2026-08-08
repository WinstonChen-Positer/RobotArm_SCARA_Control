"""SCARA 工位继电器控制封装。

2026-07-24 起改为经常驻守护进程(relay_pump_daemon.py)按**具名设备**下发，与机械臂/仪表板/
龙门统一走同一套 name-lookup 框架；映射权威源 src/devices/robot_arm/robot_arm.yaml
relay.devices：
  - 电磁阀(SCARA 泄压阀) = scara_vent  (现 b81 / ch2)
  - 泵                   = scara_pump  (现 b80 / ch2)

2026-07-26 更正：scara_vent 曾被记为 duco_io/ch2，是错的 —— 这路阀根本没接 DUCO 机柜 IO，
而是第四块塔石板 b81(192.168.0.81:1024)。它长期失控的原因是出厂默认 IP 与 b80 撞车。

旧实现直连 ``TASIO(TcpTransport('192.168.0.80', 1024))`` 且写死 ``CH_VALVE=1 / CH_PUMP=2``：
换新映射后 scara_vent 与 pump 分属两块板、旧"阀泵同板 ch1/ch2"假设失效，
点电磁阀会误开 b80/ch1=机械臂吸盘泵；且直连会与守护进程争同一 Modbus 板(双主)。改走守护进程后：
单一板主、无双主争线、随 yaml 自动适配。**对外接口(connect/disconnect/is_connected/read_states/
set_valve/set_pump/valve_on/off/pump_on/off/all_off/cleanup)保持不变**，SCARA 控制器/UI 调用方 0 改动。

前提：需 relay_pump_daemon.py 在跑(现在所有继电器都经它，本就一致)。
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import yaml

# 复用 webconsole 下已验证的守护进程 HTTP 客户端 PumpController（按 board/ch 直连 daemon:19100）
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_WEBCONSOLE = os.path.join(_REPO_ROOT, "webconsole")
if _WEBCONSOLE not in sys.path:
    sys.path.insert(0, _WEBCONSOLE)

try:
    from pump_controller import PumpController  # type: ignore
except Exception as exc:  # pragma: no cover
    raise ImportError("无法加载继电器守护进程客户端 pump_controller: %s" % exc) from exc

_RELAY_YAML = os.path.join(_REPO_ROOT, "src", "devices", "robot_arm", "robot_arm.yaml")


def _load_scara_relay_cfg() -> tuple[str, float, dict]:
    """从 robot_arm.yaml 读 daemon_url/timeout + scara_vent/scara_pump 两个具名设备映射。

    读不到时回落到与当前映射一致的内置默认(scara_vent=b81/ch2, scara_pump=b80/ch2)。
    2026-07-26：scara_vent 从 duco_io 更正为 b81 —— 这四路阀/泵从来不在 DUCO 机柜 IO 上，
    而是第四块塔石板（此前顶着出厂默认 IP 192.168.0.80 与 b80 撞车，故一直控不了）。
    这个兜底值写错的后果是 yaml 读失败时泄压阀发到不存在的板上、静默失效，故必须同步更正。
    """
    daemon_url = "http://127.0.0.1:19100"
    timeout = 3.0
    devices = {
        "scara_vent": {"board": "b81", "ch": 2},
        "scara_pump": {"board": "b80", "ch": 2},
    }
    try:
        cfg = (yaml.safe_load(open(_RELAY_YAML, encoding="utf-8")) or {}).get("relay", {})
        daemon_url = cfg.get("daemon_url", daemon_url)
        timeout = float(cfg.get("timeout_s", timeout))
        yd = cfg.get("devices") or {}
        for name in ("scara_vent", "scara_pump"):
            if isinstance(yd.get(name), dict):
                devices[name] = {
                    "board": str(yd[name].get("board", devices[name]["board"])),
                    "ch": int(yd[name].get("ch", devices[name]["ch"])),
                }
    except Exception:
        pass
    return daemon_url, timeout, devices


class ScaraRelayController:
    """SCARA 电磁阀/泵继电器控制器（非 Qt，可脱离 UI 测试）。

    电磁阀=scara_vent、泵=scara_pump，经守护进程按具名设备下发(board/ch 现场查 yaml)。
    不再自持 Modbus 连接，故 connect/disconnect 实为"校验/标记守护进程可达"。
    """

    # 具名设备键（板/通道由 yaml 决定，见 _load_scara_relay_cfg）
    VALVE_DEVICE = "scara_vent"
    PUMP_DEVICE = "scara_pump"

    def __init__(self, daemon_url: Optional[str] = None):
        url, timeout, devices = _load_scara_relay_cfg()
        self._devices = devices
        self._pc = PumpController(
            daemon_url=daemon_url or url, timeout=timeout, relay_devices=devices
        )
        self._connected = False

    # ------------------------------------------------------------------ #
    #  连接与状态（继电器板由守护进程持有；这里只校验其可达）
    # ------------------------------------------------------------------ #
    def connect(self, timeout: float = 2.0) -> bool:
        """校验守护进程可达（不再自持 Modbus 连接）。"""
        self._connected = self._pc.is_reachable()
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._pc.is_reachable()

    def read_states(self) -> tuple[bool, bool]:
        """读取 (电磁阀, 泵) 两路状态。读失败返回 (False, False)。"""
        try:
            st = self._pc.read_pumps()
            return bool(st.get(self.VALVE_DEVICE)), bool(st.get(self.PUMP_DEVICE))
        except Exception:
            return False, False

    # ------------------------------------------------------------------ #
    #  通道控制（按具名设备经守护进程下发）
    # ------------------------------------------------------------------ #
    def _set(self, device: str, on: bool) -> None:
        d = self._devices[device]
        self._pc._set(d["ch"], bool(on), d["board"])

    def set_valve(self, on: bool) -> None:
        """电磁阀(SCARA 泄压阀 scara_vent → b81/ch2)开关。"""
        self._set(self.VALVE_DEVICE, on)

    def set_pump(self, on: bool) -> None:
        """泵(scara_pump → b80/ch2)开关。"""
        self._set(self.PUMP_DEVICE, on)

    def valve_on(self) -> None:
        self.set_valve(True)

    def valve_off(self) -> None:
        self.set_valve(False)

    def pump_on(self) -> None:
        self.set_pump(True)

    def pump_off(self) -> None:
        self.set_pump(False)

    def all_off(self) -> None:
        """关闭电磁阀和泵。"""
        self.set_pump(False)
        self.set_valve(False)

    def cleanup(self) -> None:
        """关闭两路输出（守护进程连接无需显式断开）。"""
        try:
            self.all_off()
        except Exception:
            pass
        self.disconnect()
