# -*- coding: utf-8 -*-
"""热台泵/机械臂泵/继电器设备 HTTP 客户端。

直接调用 armweb（webconsole/server.py）的泵/继电器端点，不依赖机械臂后端是否已连接。
主软件用它在 orchestrator UI 里给各类泵/阀按钮独立开关 + 轮询真实 DO 状态。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request


def _normalize_base(base: str | None) -> str:
    if not base:
        return "http://127.0.0.1:8080"
    base = base.strip().rstrip("/")
    if not base.startswith("http"):
        base = "http://" + base
    return base


class PumpClient:
    """轻量 HTTP 客户端：talk to armweb /api/pump 与 /api/relay。"""

    def __init__(self, base_url: str | None = None, timeout: float = 5.0):
        self.base_url = _normalize_base(base_url)
        self.timeout = float(timeout)
        # 连接预热（卡顿缓解，见 task#3）：构造时后台线程 ping 一次 /api/status，把首连的
        # DNS/TCP 握手 + armweb 冷启动这笔一次性开销提前付掉，这样用户点第一次泵/阀开关时
        # 连接已"热"，不再出现"前几次卡顿"。urllib 每请求都新建连接、无 keep-alive，冷启动
        # 开销无法靠复用连接省掉，只能预先付。守护线程、失败无所谓（真实操作自带重试）。
        threading.Thread(target=self._warmup, daemon=True).start()

    def _warmup(self) -> None:
        try:
            self._request("/api/status", "GET", retries=1)
        except Exception:
            pass

    def _request(self, path: str, method: str = "GET", body: dict | None = None,
                 retries: int = 0) -> dict:
        url = self.base_url + path
        data = json.dumps(body or {}).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 服务器已应答（4xx/5xx）——非连接问题，不重试，尽量取回业务错误体。
                try:
                    err_body = e.read().decode("utf-8")
                    return json.loads(err_body)
                except Exception:
                    raise RuntimeError("服务器通信失败: HTTP %s" % e.code)
            except Exception as e:
                # 连接类瞬时错误（首连超时/被拒/冷启动）：短暂退避后重试。缓解"前几次开关
                # 卡顿或丢命令"。只有 SET 命令与预热开 retries，状态轮询 retries=0 快速失败，
                # 好让按钮及时变橙。继电器 set 是幂等的，重发同一值安全。
                if attempt < retries:
                    attempt += 1
                    time.sleep(0.15)
                    continue
                raise RuntimeError("服务器通信失败: %s" % e)

    def _checked_post(self, path: str, body: dict) -> None:
        # SET 命令开一次重试（retries=1）以吸收首连抖动；轮询读状态不走这里。
        d = self._request(path, "POST", body, retries=1)
        if not d.get("ok"):
            raise RuntimeError(d.get("error") or "服务器拒绝操作")

    # ── 旧 /api/pump 端点（保持兼容）─────────────────────────────────────
    def set_hotplate_pump(self, on: bool) -> None:
        """热台阀（b80 新4路板 ch3）开关（独立于机械臂连接）。等价于 set_hotplate_valve。"""
        self.set_hotplate_valve(on)

    def set_arm_pump(self, on: bool) -> None:
        """机械臂吸盘泵（b80 新4路板 ch1）开关。

        改走通用 /api/relay 端点（等价 set_relay_device("arm_pump", on)），与 big_pump / 各阀一致。
        旧实现走 /api/pump {which:"arm"} → 服务器 set_arm_pump()，那条强制机械臂已连接+使能，
        未连时静默失败、继电器不动（task#2 的 bug 根因）。改走 relay 后不再查机械臂就绪。
        """
        self.set_relay_device("arm_pump", on)

    # ── 新 /api/relay 通用端点────────────────────────────────────────────
    # 板/通道见 robot_arm.yaml relay.devices（2026-07-26 逐路通断实测确认）：
    #   b81(塔石网络板 192.168.0.81:1024)：ch1=vent_valve(DUCO泄压阀) ch2=scara_vent(SCARA泄压阀)
    #       ch3=peel_valve(胶带玻璃台泵，是泵不是阀) ch4=hotplate_valve(热台泵，是泵不是阀)
    #       ※ 这四路曾被记为 duco_io/502，是错的；它们从来不在 DUCO 机柜 IO 上。
    #   b80(塔石网络板 192.168.0.80:1024)：ch1=arm_pump ch2=scara_pump ch3=alarm_red(红+蜂鸣) ch4=alarm_yellow(黄)
    #   b50(旧连云板 192.168.1.50)：ch1=alarm_green(绿) ch2=big_pump(大泵)
    #   共 10 路物理通道；状态里的 hotplate_pump 不是独立通道，只是 hotplate_valve 的别名镜像。
    def set_relay_device(self, name: str, on: bool) -> None:
        """通用继电器设备开关（name 对应 robot_arm.yaml relay.devices 的键名）。"""
        self._checked_post("/api/relay", {"name": name, "value": bool(on)})

    def set_big_pump(self, on: bool) -> None:
        """大泵 / 共用真空源（b50 旧2路板 ch2）。"""
        self.set_relay_device("big_pump", on)

    def set_hotplate_valve(self, on: bool) -> None:
        """热台电磁阀（b80 新4路板 ch3）。"""
        self.set_relay_device("hotplate_valve", on)

    def set_peel_valve(self, on: bool) -> None:
        """剥离台电磁阀 / 揭膜阀（b80 新4路板 ch2）。"""
        self.set_relay_device("peel_valve", on)

    def set_vent_valve(self, on: bool) -> None:
        """机械臂减压阀（b50 旧2路板 ch1）。"""
        self.set_relay_device("vent_valve", on)

    def set_scara_vent(self, on: bool) -> None:
        """SCARA 系统泄压阀（b80 新4路板 ch4）。"""
        self.set_relay_device("scara_vent", on)

    def set_scara_pump(self, on: bool) -> None:
        """SCARA 气泵（第三板 new1 串口 TAS-IO DI1）。单独开关泵；
        与 scara_vent 组成真空对，联动请用 scara_suction_on/off。"""
        self.set_relay_device("scara_pump", on)

    # ── SCARA 真空对封装（scara_pump + scara_vent）────────────────────────
    #    真空吸取模式（用户确认 2026-07-17）：建真空=泵开+泄压阀关；破真空快放=泵关+泄压阀开。
    #    与机械臂 arm_pump/vent_valve 同款。供流程「配合使用」，也可仪表板一键调用。
    def scara_suction_on(self) -> None:
        """SCARA 建真空吸取：先关泄压阀（防漏气）再开气泵。"""
        self.set_scara_vent(False)
        self.set_scara_pump(True)

    def scara_suction_off(self) -> None:
        """SCARA 破真空释放：先关气泵再开泄压阀（快速泄压放料）。"""
        self.set_scara_pump(False)
        self.set_scara_vent(True)

    def set_hotplate_vent(self, on: bool) -> None:
        """⚠️ 已弃用：热台泄压阀无物理通道，热台破真空改经共用管路的剥离台阀(peel_valve)。
        为兼容旧调用方，本方法透明重定向到 peel_valve，不再下发已废弃的 hotplate_vent。"""
        self.set_relay_device("peel_valve", on)

    # ── 三色信号灯（继电器输出）2026-07-24 新增 ──────────────────────────
    def set_alarm_green(self, on: bool) -> None:
        """绿灯（b50 ch1）：就绪 / 运行指示。"""
        self.set_relay_device("alarm_green", on)

    def set_alarm_yellow(self, on: bool) -> None:
        """黄灯（b80 ch4）：警告指示。"""
        self.set_relay_device("alarm_yellow", on)

    def set_alarm_red(self, on: bool) -> None:
        """红灯 + 蜂鸣器（b80 ch3）：报警指示。"""
        self.set_relay_device("alarm_red", on)

    def read_pumps(self) -> dict:
        """读取真实 DO 状态。优先读 /api/relay/status，失败降级到 /api/pump/status。"""
        d = self._request("/api/relay/status")
        if not d.get("ok"):
            d = self._request("/api/pump/status")
            if not d.get("ok"):
                raise RuntimeError(d.get("error") or "读泵状态失败")
        # 服务器 armweb 用嵌套 {"devices":{...}}，旧版本地服务是扁平结构——两种都兼容
        s = d.get("devices", d)
        return {
            "arm_pump": bool(s.get("arm_pump", False)),
            "big_pump": bool(s.get("big_pump", False)),
            "vent_valve": bool(s.get("vent_valve", False)),
            "hotplate_valve": bool(s.get("hotplate_valve", False)),
            "scara_vent": bool(s.get("scara_vent", False)),
            "scara_pump": bool(s.get("scara_pump", False)),
            "hotplate_vent": bool(s.get("hotplate_vent", False)),  # 已无通道，恒 False
            "hotplate_pump": bool(s.get("hotplate_pump", s.get("hotplate_valve", False))),
            "peel_valve": bool(s.get("peel_valve", False)),
        }

    # ── 夹爪（大寰 DH PGEA-50-26-O）─────────────────────────────────────
    #    经 armweb /api/gripper/* 端点控制（服务器侧走 DUCO 工具端 485 透传 Modbus 帧）。
    #    夹爪只设到一个固定开合位（不参与取放硅片，取放仍走侧面吸盘），故这里给仪表板
    #    「夹爪」卡片提供手动 初始化 / 设位置 / 全开 三个动作 + 状态读取。取值范围钳制交
    #    服务器，本地只做 int 归一；SET 走 _checked_post（开一次重试吸首连抖动，与继电器一致）。
    def gripper_init(self, full: bool = False) -> None:
        """夹爪初始化（回零标定）。full=False→单向(0x01,快)/True→全行程(0xA5,完整标定)。
        上电首次必做。POST /api/gripper/init {full}；失败抛 RuntimeError。"""
        self._checked_post("/api/gripper/init", {"full": bool(full)})

    def gripper_grip(self, pos: int, force: int = 50, speed: int = 50) -> None:
        """夹爪设到位置。pos 0–1000‰（0=闭合(夹紧) / 1000=全开），force 20–100%，speed 1–100%。

        POST /api/gripper/grip {pos,force,speed}；失败抛 RuntimeError。
        """
        self._checked_post(
            "/api/gripper/grip",
            {"pos": int(pos), "force": int(force), "speed": int(speed)},
        )

    def gripper_release(self, speed: int = 50) -> None:
        """夹爪全开释放（机械张开，不需破真空）。speed 1–100%。POST /api/gripper/release {speed}。"""
        self._checked_post("/api/gripper/release", {"speed": int(speed)})

    def gripper_status(self) -> dict | None:
        """读夹爪状态（armweb 把夹爪字段并入 GET /api/status）。

        返回 {"init_state", "grip_state", "position", "error"}（各值为 int，缺失/非数字则该项为 None）：
          - init_state 0=未初始化 / 1=初始化成功 / 2=初始化中
          - grip_state 0=运动中 / 1=到位未夹到 / 2=夹住 / 3=物体掉落
          - position   0–1000‰
          - error      0=无报警 / 非0=报警码（0x0205）
        全字段缺失、或 /api/status 读失败时返回 None（调用方据此显示「—」，不报错）。
        兼容两种键名：服务器嵌套 {"gripper":{init_state,...}} 与旧扁平 gripper_init_state 前缀。
        """
        try:
            d = self._request("/api/status")
        except Exception:
            return None
        if not isinstance(d, dict):
            return None
        src = d.get("gripper") if isinstance(d.get("gripper"), dict) else d
        if not isinstance(src, dict):
            return None

        def _as_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        def _pick(*names):
            for n in names:
                if n in src:
                    return _as_int(src.get(n))
            return None

        recognized = ("init_state", "grip_state", "position", "error",
                      "gripper_init_state", "gripper_grip_state",
                      "gripper_position", "gripper_error")
        if not any(k in src for k in recognized):
            return None
        return {
            "init_state": _pick("init_state", "gripper_init_state"),
            "grip_state": _pick("grip_state", "gripper_grip_state"),
            "position": _pick("position", "gripper_position"),
            "error": _pick("error", "gripper_error"),
        }


def load_pump_base_url(config_path: str | None = None) -> str:
    """从 robot_arm.yaml 或用户配置里读 armweb_base_url；没有则返回默认。"""
    try:
        from devices.robot_arm import load_config
    except Exception:
        return _normalize_base(None)
    cfg = load_config()
    return _normalize_base(
        cfg.get("armweb_base_url")
        or cfg.get("connection", {}).get("armweb_base_url")
        or None)
