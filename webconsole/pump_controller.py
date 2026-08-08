# -*- coding: utf-8 -*-
"""真空泵/继电器设备 HTTP 客户端（冒充云接管方案）。

继电器保持出厂"连云客户端"模式不改（切 TCP 服务端无可靠自动化路径：官方工具走 UDP
私有协议+GUI，Modbus 改网络寄存器不可靠）。改由常驻守护进程 relay_pump_daemon.py
冒充云、接住继电器、用可靠的 DO 线圈读写控泵/阀，并暴露 HTTP 接口。

本类是该守护进程的 HTTP 客户端。支持双板（板键 b50/b80，见 robot_arm.yaml relay.devices）：
  POST /relay {board:'b50'|'b80', ch, on}
  GET  /relays?board=<b50|b80>&n=<ch_count>
对外保留 set_arm_pump/set_hotplate_pump/read_pumps/is_reachable/close 等旧接口，
同时支持具名设备映射（relay_devices）。线程安全（stdlib urllib 每请求独立），失败重试。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Optional

ARM_PUMP_DO = 1       # 旧 DO 号，仅未提供具名映射时的降级用（现 arm_pump=b80 ch1）
HOTPLATE_PUMP_DO = 2  # 旧 DO 号，仅降级用（现 hotplate_valve=b80 ch3）

# 2026-07-26 逐路通断实测确认的默认具名映射（可被 robot_arm.yaml relay.devices 覆盖；
# 本常量仅作文档/降级参考——PumpController 实例实际用构造时传入的 relay_devices）。
# 板键：b50=192.168.1.50(旧2路板)、b80=192.168.0.80(4路板)、b81=192.168.0.81(4路板)。
# ※ 前 4 路曾被记为 duco_io(DUCO 机柜自带 IO 192.168.1.10:502)，是错的 —— 它们从来不在
#   DUCO 那一路，而是第四块塔石板 b81。b81 长期顶着出厂默认 IP 192.168.0.80 与 b80 撞车，
#   软件够不着，所以这四路一直控不了；2026-07-26 改 IP 到 .81 后实测全部通断正常。
DEFAULT_RELAY_DEVICES = {
    # 继电器4 (b81 塔石网络板 192.168.0.81:1024) — 2 泄压阀 + 2 泵
    "vent_valve":     {"board": "b81", "ch": 1},   # DUCO 机械臂泄压阀
    "scara_vent":     {"board": "b81", "ch": 2},   # SCARA 机械臂泄压阀
    "peel_valve":     {"board": "b81", "ch": 3},   # 胶带玻璃台泵（是泵不是阀）
    "hotplate_valve": {"board": "b81", "ch": 4},   # 热台泵（是泵不是阀）
    # 继电器2 (b80 192.168.0.80:1024) — 泵 + 红/黄灯
    "arm_pump":       {"board": "b80", "ch": 1},   # 泵1-duco
    "scara_pump":     {"board": "b80", "ch": 2},   # 泵2-scara吸盘
    "alarm_red":      {"board": "b80", "ch": 3},   # 红灯+蜂鸣器
    "alarm_yellow":   {"board": "b80", "ch": 4},   # 黄灯
    # 继电器3 (b50 旧连云板 192.168.1.50) — 绿灯 + 大泵
    "alarm_green":    {"board": "b50", "ch": 1},   # 绿灯
    "big_pump":       {"board": "b50", "ch": 2},   # 大泵
}


class PumpError(Exception):
    pass


class PumpController:
    def __init__(self, daemon_url: str = "http://127.0.0.1:19100",
                 timeout: float = 3.0, retries: int = 2,
                 arm_pump_do: int = ARM_PUMP_DO, hotplate_pump_do: int = HOTPLATE_PUMP_DO,
                 relay_devices: Optional[dict] = None,
                 # 兼容旧签名（host/port/address）—— 忽略，仅避免调用方报错
                 host: Optional[str] = None, port: Optional[int] = None,
                 address: Optional[int] = None, **_ignored):
        self._url = daemon_url.rstrip("/")
        self._timeout = float(timeout)
        self._retries = int(retries)
        # 保留旧 DO 号，用于未提供具名映射时的降级
        self._arm_do = int(arm_pump_do)
        self._hp_do = int(hotplate_pump_do)
        self._lock = threading.Lock()
        if relay_devices:
            self._devices = {
                name: {"board": str(cfg.get("board", "b80")), "ch": int(cfg["ch"])}
                for name, cfg in relay_devices.items()
            }
        else:
            # 无具名映射时退化为旧行为（board 默认 b80=新4路板）
            self._devices = {
                "arm_pump": {"board": "b80", "ch": self._arm_do},
                "hotplate_pump": {"board": "b80", "ch": self._hp_do},
            }

    # ── HTTP（带重试）──────────────────────────────────────────
    def _request(self, path: str, body: Optional[dict] = None,
                 board: Optional[str] = None) -> dict:
        """发 HTTP 请求。GET 时若指定 board 会补到 query；POST 时 board 由调用方写入 body。"""
        if board is not None and body is None:
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}board={board}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        last = None
        for attempt in range(self._retries + 1):
            try:
                req = urllib.request.Request(self._url + path, data=data,
                                             headers=headers,
                                             method="POST" if data else "GET")
                with urllib.request.urlopen(req, timeout=self._timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                last = e
                if attempt < self._retries:
                    time.sleep(0.1)
        raise PumpError("继电器守护进程不可达: %s" % last)

    def _set(self, ch: int, on: bool, board: str = "b80"):
        with self._lock:
            r = self._request("/relay", {"board": board, "ch": int(ch), "on": bool(on)})
        if not r.get("ok"):
            raise PumpError(r.get("error") or "继电器拒绝")

    # ── 对外 API（与旧版一致，但优先走具名映射）──────────────────
    def set_arm_pump(self, on: bool) -> None:
        dev = self._devices.get("arm_pump")
        if dev is None:
            dev = {"board": "b80", "ch": self._arm_do}
        self._set(dev["ch"], on, dev["board"])

    def set_hotplate_pump(self, on: bool) -> None:
        # 旧接口语义迁移：优先对应热台阀（b80 新4路板 ch3）
        dev = self._devices.get("hotplate_pump") or self._devices.get("hotplate_valve")
        if dev is None:
            dev = {"board": "b80", "ch": self._hp_do}
        self._set(dev["ch"], on, dev["board"])

    def read_pumps(self) -> dict:
        """读取所有已配置继电器设备的实际 DO 状态。

        按 board 分组、按最大通道号一次性读取，减少 HTTP 往返。
        返回键名与配置中的 device 名一致（旧配置时仍为 arm_pump/hotplate_pump）。
        """
        boards: dict[str, int] = {}
        for dev in self._devices.values():
            boards[dev["board"]] = max(boards.get(dev["board"], 0), dev["ch"])
        board_states: dict[str, list[bool]] = {}
        with self._lock:
            for board, n in boards.items():
                # 单板读失败(如 b50 连云未起)不整体抛异常：该板设备退化为 False，其余板照常。
                # 否则一块离线板会拖垮整个继电器状态轮询、UI 全灭（优雅降级）。
                try:
                    r = self._request("/relays?n=%d" % n, board=board)
                except PumpError:
                    r = {"ok": False}
                board_states[board] = list(r.get("relays") or []) if r.get("ok") else []
        result: dict[str, bool] = {}
        for name, dev in self._devices.items():
            states = board_states.get(dev["board"], [])
            idx = dev["ch"] - 1
            result[name] = bool(states[idx]) if 0 <= idx < len(states) else False
        # 兼容旧前端：若只有 hotplate_valve 没有 hotplate_pump，返回同名键
        if "hotplate_valve" in result and "hotplate_pump" not in result:
            result["hotplate_pump"] = result["hotplate_valve"]
        return result

    def is_reachable(self) -> bool:
        try:
            r = self._request("/health")
            return bool(r.get("ok")) and bool(r.get("connected"))
        except Exception:
            return False

    def close(self):
        pass  # HTTP 无常驻连接


_INSTANCE: Optional[PumpController] = None
_INSTANCE_LOCK = threading.Lock()


def get_pump_controller(daemon_url: str = "http://127.0.0.1:19100", **kw) -> PumpController:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = PumpController(daemon_url, **kw)
        return _INSTANCE
