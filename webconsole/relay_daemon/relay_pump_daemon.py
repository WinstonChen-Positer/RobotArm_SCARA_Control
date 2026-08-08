# -*- coding: utf-8 -*-
"""常驻继电器泵守护进程（冒充云接管方案，跨平台）。

继电器保持出厂"连云客户端"模式不改。本守护进程冒充云、接住继电器连接、自动重连，
用可靠的 DO 线圈读写控制两个泵，并暴露最小 HTTP 接口给 armweb 调用。

为什么这样：官方工具用 UDP 私有协议 + GUI 配网络（无法自动化）；Modbus 改网络寄存器
不可靠。但 DO 线圈 FC01/FC05 读写可靠（已端到端验证）。故不碰网络配置，只用 DO 控制。

平台：
  - Windows（本机开发/测试）：scapy + npcap 发 DHCP（tas_io_ethernet.CloudServerTransport）。
  - Linux（生产服务器）：dnsmasq 发 DHCP + ip 别名（linux_cloud_transport.LinuxCloudTransport）。
  两条路径的 TCP 接受 + Modbus 透传完全一致；对上层（RelayLink/HTTP）透明。

HTTP 接口（默认 127.0.0.1:19100）：
  GET  /health           → {"ok":bool,"connected":bool,"peer":...}
  GET  /relays?n=2       → {"ok":bool,"relays":[bool,...]}
  POST /relay {"ch":1,"on":true} → {"ok":bool,"error":...}

跑法：
  # Linux 服务器（需 root + dnsmasq）：
  sudo python3 relay_pump_daemon.py --platform linux --iface enp97s0
  # Windows 本机（需 npcap；继电器接本机、已上电）：
  venv\\Scripts\\python.exe relay_pump_daemon.py --platform windows --nic I225
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 让"以脚本方式在本目录运行"和"作为包导入"都能找到同目录模块
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from tas_io import TASIO, ModbusError

# 默认设备参数（可被命令行覆盖）
DEFAULT_DEVICE_MAC = "3c:ab:72:02:93:76"
DEFAULT_ADDR = 0x11
DEFAULT_LISTEN_PORT = 10067
DEFAULT_OFFER_IP = "192.168.0.80"
DEFAULT_SERVER_IP = "192.168.0.1"
DEFAULT_CLOUD_IP = "47.99.152.116"

# 多板配置（2026-07-17 按用户逐路实测确认重排）。键名用 IP 末段(b50/b80)彻底消除"新/旧"歧义——
# 历史 new/old 键与物理板正好相反、曾导致点错设备（点大泵开的是别的板），故改用 IP 段命名、可直接 ping 对上。
# 两块板工作模式不同：
#   b50 (192.168.1.50, 用户口中"旧继电器", 2通道)：TCP Client 模式，主动拨 47.99.152.116:10067，
#        本机冒充云接住(需 npcap/管理员)。站号 0x11。DI1=机械臂减压阀 DI2=大泵。
#        走 MultiCloudServerTransport(mode="server"=板拨我)。
#   b80 (192.168.0.80, 用户口中"新继电器", 4通道)：TCP Server 模式，监听 :10123，站号 0x01。
#        本机主动连它(ClientBoardTransport,普通socket,不需npcap)。DI1=机械臂泵 DI2=剥离台电磁阀
#        DI3=热台电磁阀 DI4=SCARA泄压阀。走 mode="client"=我连板。
#   new1 (第三块板"新1", SCARA系统, DI1=SCARA泵)：链路灯未亮/IP未定，暂不启用。上线后在此加一条
#        {"mode":..,"ip":..,"addr":..,"channels":1,..} 并把 robot_arm.yaml 里 scara_pump 取消注释。
# mode: "server"=板拨我(冒充云) / "client"=我连板(直连其 TCP Server)。
# 2026-07-24 新映射（用户重排 + 泵测法逐板确认）：
#   继电器1            = duco_io  DUCO 机械臂自带 IO（标准 Modbus TCP 502）
#     ※ 曾以为那 4 路阀/泵接在这块上，2026-07-26 证实是错的：它们在 b81。duco_io 现不承载任何设备，
#       保留仅因它确实在线、以后要用 DUCO 自带 IO 时现成。
#   继电器2(泵+灯)     = b80      塔石网络板（标准 Modbus TCP 1024，实测口=1024，非旧 10123）
#   继电器3(绿灯+大泵) = b50      旧连云板（板主动拨云，daemon 冒充云在 10067 accept）
# 2026-07-26 补第 4 块板 b81（4 路阀/泵）。它一直控不了的根因是**出厂默认没配过**：
#   IP 停在塔石出厂默认 192.168.0.80，和 b80 撞车（对该 IP 发 ARP 两块板全抢答，
#   缓存里是谁数据就发给谁），且监听口是 10123 而非软件连的 1024。已用二层直连
#   （tools/relay_valve_l2.py，绕开 ARP 冲突）读到它的 Web 配置，经
#   tools/relay_valve_configure.py 改为 192.168.0.81:1024、波特率对齐 b80，
#   实测标准 MBAP 读 4 路线圈成功。原始配置备份在 tools/relay_valve_backup.json。
DEFAULT_BOARDS = {
    # 继电器1: DUCO 机械臂自带 8 路 IO。标准 MBAP:502，绑源 .200 走交换机网卡。
    # 当前不承载任何具名设备（那 4 路阀/泵实为 b81），保留备用。
    "duco_io": {"mode": "mbap", "ip": "192.168.1.10", "port": 502,
                "src_ip": "192.168.1.200", "addr": 0x01, "channels": 4},
    # 继电器2: b80 塔石网络板。标准 MBAP:1024，绑源 .0.1。
    "b80": {"mode": "mbap", "ip": "192.168.0.80", "port": 1024,
            "src_ip": "192.168.0.1", "addr": 0x01, "channels": 4},
    # 继电器3: b50 旧连云板。板拨云，daemon 冒充云在 10067 accept（需 scapy+npcap+管理员）。
    "b50": {"mode": "server", "ip": "192.168.1.50", "mac": "3c:ab:72:02:93:76",
            "addr": 0x11, "channels": 2, "dhcp": False},
    # 继电器4: b81 塔石网络板（与 b80 同型同固件，MAC 34:46:63:d2:0e:c7）。
    # 4 路：1 DUCO 泄压阀 / 2 SCARA 泄压阀 / 3 胶带玻璃台泵 / 4 热台泵。
    "b81": {"mode": "mbap", "ip": "192.168.0.81", "port": 1024,
            "src_ip": "192.168.0.1", "addr": 0x01, "channels": 4},
}
DEFAULT_BOARD_NAME = "b50"   # 请求未带 board 时的默认板


class RelayLink:
    """持有继电器冒充云连接，线程安全，断连自动重连。

    transport 已构造好（平台相关），本类只负责：后台确保有连接、线程安全地读写 DO。
    """

    def __init__(self, transport, address: int = DEFAULT_ADDR):
        self._tr = transport
        self._addr = int(address)
        self._lock = threading.Lock()
        self._dev: TASIO | None = None
        self._peer = None
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        """后台不断确保有继电器连接：断了就重新 wait_for_device。"""
        while True:
            if self._dev is None:
                try:
                    peer = self._tr.wait_for_device(timeout=3600)
                    with self._lock:
                        self._dev = TASIO(self._tr, address=self._addr)
                        self._peer = peer
                    print(f"[link] 继电器已连接: {peer}", flush=True)
                except Exception as e:
                    print(f"[link] 等待继电器超时/失败: {e}", flush=True)
                    time.sleep(1)
            else:
                time.sleep(0.5)

    def _drop(self):
        """判定连接不可用：清 _dev + 关闭旧 socket，逼继电器重连（保留 dnsmasq+别名）。"""
        with self._lock:
            self._dev = None
            self._peer = None
        # 关键：关掉旧 TCP，否则继电器那条连接还活着不会重连，daemon 永远 accept 不到（死锁）
        try:
            reset = getattr(self._tr, "reset_conn", None)
            if reset:
                reset()
            else:
                self._tr.close()
        except Exception as e:
            print(f"[link] reset_conn 异常: {e}", flush=True)

    def connected(self) -> bool:
        return self._dev is not None

    def peer(self):
        return self._peer

    def _op(self, fn):
        """执行一次 DO 读写。区分偶发 Modbus 丢帧（同连接重试，不断连）与真 socket 断（drop 重连）。

        继电器偶发无响应/CRC 错（忙、EtherCAT 抖动）很常见，不代表连接坏——先在同一连接
        重试几次；socket 层异常才判连接断。避免"一次丢帧 → drop → 死锁 → 泵永久失联"。
        """
        with self._lock:
            dev = self._dev
        if dev is None:
            raise ModbusError("继电器未连接（等待其连云中）")
        last = None
        for attempt in (1, 2, 3):
            try:
                return fn(dev)
            except ModbusError as e:      # 无响应/CRC：偶发，同连接重试
                last = e
                time.sleep(0.05)
            except (OSError, ConnectionError, BrokenPipeError) as e:  # socket 真断
                self._drop()
                raise ModbusError("继电器 socket 断开，重连: %s" % e)
        # 同连接重试多次仍无响应 → 可能连接半死，drop 逼重连
        self._drop()
        raise ModbusError("继电器多次无响应，已重连: %s" % last)

    def set_relay(self, ch: int, on: bool):
        self._op(lambda dev: dev.set_relay(int(ch), bool(on)))

    def read_relays(self, n: int):
        return self._op(lambda dev: dev.read_relays(int(n)))


class RelayHub:
    """多板管理：{board_name: {'ip','addr','channels'}}，按 board 名路由 DO 读写。

    共用一个 MultiCloudServerTransport（同端口多连接）。每块板一个 TASIO（按需建/掉线清）。
    偶发丢帧同连接重试（不断连），socket 真断才丢弃该板 TASIO 逼重连。
    """

    def __init__(self, multi, boards: dict):
        self._multi = multi              # MultiCloudServerTransport(server 板)；可能为 None
        self._boards = boards
        self._devs: dict = {}
        self._client_trs: dict = {}      # board -> ClientBoardTransport/SerialBoardTransport
        self._serial_ports: dict = {}    # board -> 已自动认到的串口口（serial 板；防口漂移）
        self._lock = threading.Lock()

    def _dev(self, board: str):
        cfg = self._boards.get(board)
        if cfg is None:
            raise ModbusError(f"未知继电器板: {board}（有效: {list(self._boards)}）")
        mode = cfg.get("mode", "server")
        with self._lock:
            dev = self._devs.get(board)
            if dev is not None:
                return dev
            if mode == "serial":
                from tas_io_ethernet import SerialBoardTransport, find_serial_board_port
                tr = self._client_trs.get(board)
                if tr is None:
                    port = self._serial_ports.get(board) or cfg.get("port")
                    if not port or port == "auto":
                        port = find_serial_board_port(
                            baud=int(cfg.get("baud", 9600)),
                            addr=int(cfg["addr"]),
                            channels=int(cfg.get("channels", 1)))
                        if not port:
                            raise ModbusError(
                                f"串口继电器板 {board} 未找到（检查供电9~36V/AB极性/USB插好）")
                    self._serial_ports[board] = port
                    tr = SerialBoardTransport(port, baud=int(cfg.get("baud", 9600)))
                    self._client_trs[board] = tr
                dev = TASIO(tr, address=int(cfg["addr"]))
            elif mode == "client":
                from tas_io_ethernet import ClientBoardTransport
                tr = self._client_trs.get(board)
                if tr is None:
                    tr = ClientBoardTransport(cfg["ip"], cfg.get("port", 10123))
                    self._client_trs[board] = tr
                dev = TASIO(tr, address=int(cfg["addr"]))
            elif mode == "mbap":
                # 标准 Modbus TCP (MBAP)：b80(塔石网络板 1024) / DUCO 机械臂自带 IO(502)。
                # PC 主动连板子；可选 src_ip 绑源网卡（多网卡/多网段路由歧义时）。
                from tas_io import TcpMbapTransport
                tr = self._client_trs.get(board)
                if tr is None:
                    tr = TcpMbapTransport(cfg["ip"], int(cfg.get("port", 502)),
                                          src_ip=cfg.get("src_ip"))
                    self._client_trs[board] = tr
                dev = TASIO(tr, address=int(cfg["addr"]))
            else:
                ip = cfg["ip"]
                if self._multi is None or ip not in self._multi.peers():
                    raise ModbusError(f"继电器板 {board} ({ip}) 未连接")
                dev = TASIO(self._multi.board_channel(ip), address=int(cfg["addr"]))
            self._devs[board] = dev
            return dev

    def _reset(self, board: str):
        cfg = self._boards.get(board, {})
        mode = cfg.get("mode", "server")
        with self._lock:
            self._devs.pop(board, None)
            tr = self._client_trs.get(board)
            if mode in ("serial", "client", "mbap"):
                # ★根因修复(2026-07-28)：mbap/client 板也必须清掉缓存 transport（不只是下面 close），
                #   否则 _dev 会复用已 close 的 socket → 板一次断线就永久失联、只能重启整个 daemon
                #   （今晚 b80/b81 grip/放片反复失联的总根源）。清掉后 _dev 会重建新连接=自动重连。
                self._client_trs.pop(board, None)
                if mode == "serial":
                    # 串口板另清已认口，逼下次重新自动认口（防口漂移/重插）
                    self._serial_ports.pop(board, None)
        if mode in ("client", "serial", "mbap"):
            if tr is not None:
                try:
                    tr.close()
                except Exception:
                    pass
        elif self._multi is not None:
            try:
                self._multi._drop(cfg.get("ip"))
            except Exception:
                pass

    def _op(self, board: str, fn):
        # 偶发 Modbus 丢帧：同连接重试。socket 真断：_reset（清 transport 逼重连）后**重取 dev 再试**，
        # 一次调用内自愈——不再需要上层重启 daemon（根因修复 2026-07-28）。
        last = None
        for _ in (1, 2, 3):
            try:
                dev = self._dev(board)
                return fn(dev)
            except ModbusError as e:      # 无响应/CRC：偶发，同连接重试
                last = e
                time.sleep(0.05)
            except (OSError, ConnectionError, BrokenPipeError) as e:  # socket 真断：重连后重试
                last = e
                self._reset(board)
                time.sleep(0.1)
        self._reset(board)
        raise ModbusError(f"板 {board} 多次失败: {last}")

    def set_relay(self, board: str, ch: int, on: bool):
        self._op(board, lambda dev: dev.set_relay(int(ch), bool(on)))

    def read_relays(self, board: str, n: int):
        return self._op(board, lambda dev: dev.read_relays(int(n)))

    def status(self) -> dict:
        from tas_io_ethernet import tcp_reachable
        peers = self._multi.peers() if self._multi is not None else []
        out = {}
        for name, cfg in self._boards.items():
            mode = cfg.get("mode", "server")
            if mode == "serial":
                # 串口板无 IP。只在已认到口后才探活（避免 /health 触发全口扫描变慢）：
                # 读一帧线圈成功=在线；没认到口/读失败=离线。失败会经 _op 触发 _reset 重认口。
                if self._serial_ports.get(name):
                    try:
                        self.read_relays(name, int(cfg.get("channels", 1)))
                        conn = True
                    except Exception:
                        conn = False
                else:
                    conn = False
                out[name] = {"port": self._serial_ports.get(name) or cfg.get("port"),
                             "mode": mode, "connected": conn,
                             "channels": cfg.get("channels")}
                continue
            if mode in ("client", "mbap"):
                conn = tcp_reachable(cfg["ip"], cfg.get("port", 10123))
            else:
                conn = cfg["ip"] in peers
            out[name] = {"ip": cfg["ip"], "mode": mode, "connected": conn,
                         "channels": cfg.get("channels")}
        return out


LINK: RelayLink | None = None
HUB: "RelayHub | None" = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/health":
            if HUB is not None:
                st = HUB.status()
                return self._json({"ok": True, "boards": st,
                                   "connected": any(b["connected"] for b in st.values())})
            return self._json({"ok": True, "connected": LINK.connected(),
                               "peer": list(LINK.peer()) if LINK.peer() else None})
        if self.path.startswith("/relays"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            n = int((q.get("n") or ["2"])[0])
            board = (q.get("board") or [None])[0]
            try:
                if HUB is not None:
                    board = board or DEFAULT_BOARD_NAME
                    return self._json({"ok": True, "board": board, "relays": HUB.read_relays(board, n)})
                return self._json({"ok": True, "relays": LINK.read_relays(n)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        return self._json({"ok": False, "error": "unknown"}, 404)

    def do_POST(self):
        if self.path == "/relay":
            b = self._body()
            try:
                if HUB is not None:
                    board = b.get("board") or DEFAULT_BOARD_NAME
                    HUB.set_relay(board, int(b.get("ch")), bool(b.get("on")))
                else:
                    LINK.set_relay(int(b.get("ch")), bool(b.get("on")))
                return self._json({"ok": True, "error": ""})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        return self._json({"ok": False, "error": "unknown"}, 404)


def make_transport(args):
    """按平台构造冒充云传输层。"""
    plat = args.platform
    if plat == "auto":
        plat = "linux" if sys.platform.startswith("linux") else "windows"

    if plat == "linux":
        from linux_cloud_transport import LinuxCloudTransport
        print(f"[init] Linux 传输：iface={args.iface} dnsmasq DHCP", flush=True)
        return LinuxCloudTransport(
            iface=args.iface, device_mac=args.device_mac,
            offer_ip=args.offer_ip, server_ip=args.server_ip,
            cloud_ip=args.cloud_ip, listen_port=args.listen_port,
            auto_net=not args.no_auto_net,
            host_ip_cidr=args.host_ip)

    # windows
    from tas_io_ethernet import CloudServerTransport, find_nic_mac
    nic_mac = find_nic_mac(args.nic)
    if not nic_mac:
        raise RuntimeError(f"找不到网卡 {args.nic}（需 npcap + 有线网卡）")
    print(f"[init] Windows 传输：nic={args.nic} ({nic_mac}) scapy DHCP", flush=True)
    return CloudServerTransport(
        listen_port=args.listen_port, device_mac=args.device_mac,
        offer_ip=args.offer_ip, server_ip=args.server_ip, nic_mac=nic_mac)


def main():
    global LINK, HUB
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["auto", "linux", "windows"], default="auto")
    ap.add_argument("--http-port", type=int, default=19100)
    ap.add_argument("--http-host", default="127.0.0.1")
    ap.add_argument("--iface", default="enp97s0", help="Linux 有线网卡名")
    ap.add_argument("--nic", default="I225", help="Windows 网卡描述关键字")
    ap.add_argument("--device-mac", default=DEFAULT_DEVICE_MAC)
    ap.add_argument("--address", type=lambda s: int(s, 0), default=DEFAULT_ADDR)
    ap.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT)
    ap.add_argument("--offer-ip", default=DEFAULT_OFFER_IP)
    ap.add_argument("--server-ip", default=DEFAULT_SERVER_IP)
    ap.add_argument("--cloud-ip", default=DEFAULT_CLOUD_IP)
    ap.add_argument("--no-auto-net", action="store_true",
                    help="Linux：不自动配置网卡别名（由外部/systemd 预先配好）")
    ap.add_argument("--host-ip", default="192.168.1.100/24",
                    help="Linux：服务器在继电器网段的主IP/CIDR，daemon 启动时补加（服务器重启会丢，补加保留到继电器的直连路由）")
    ap.add_argument("--single", action="store_true",
                    help="强制单板旧模式（默认 Windows 走多板 MultiCloudServerTransport，同端口按 board 路由 old/new 两块板）")
    args = ap.parse_args()

    print("=" * 55, flush=True)
    print("继电器泵守护进程（冒充云接管，跨平台）", flush=True)
    print("=" * 55, flush=True)

    plat = args.platform
    if plat == "auto":
        plat = "linux" if sys.platform.startswith("linux") else "windows"

    if plat == "windows" and not args.single:
        # 多板模式：MBAP 板(duco_io/b80/b81)走普通 socket 无需 npcap；仅 server 模式板(b50 连云)需 npcap+管理员。
        # 连云传输起不来时（无 npcap/非管理员/网卡缺失）优雅降级：MBAP 板照常工作，server 板显示离线。
        boards = DEFAULT_BOARDS
        transport = None
        if any(cfg.get("mode", "server") == "server" for cfg in boards.values()):
            try:
                from tas_io_ethernet import MultiCloudServerTransport, find_nic_mac
                nic_mac = find_nic_mac(args.nic)
                if not nic_mac:
                    raise RuntimeError(f"找不到网卡 {args.nic}（需 npcap + 有线网卡）")
                dhcp_offers = {cfg["mac"]: cfg.get("offer", args.offer_ip)
                               for cfg in boards.values() if cfg.get("dhcp")}
                print(f"[init] 连云模式 nic={args.nic} ({nic_mac})  板={list(boards)}  DHCP={dhcp_offers}", flush=True)
                transport = MultiCloudServerTransport(
                    listen_port=args.listen_port, dhcp_offers=dhcp_offers,
                    server_ip=args.server_ip, nic_mac=nic_mac)
            except Exception as e:
                print(f"[init] 连云传输未起（server 板 b50 绿灯/大泵暂不可控，MBAP 板正常）: {e}", flush=True)
                transport = None
        HUB = RelayHub(transport, boards)
        # 第三板(串口)开机 best-effort 自动认口 + 预热：认到则 /health 直接在线；
        # 没接通（供电/AB/USB）只记日志、不影响网络板，用户修好硬件后首次控制即自动认口。
        for _bn, _bc in boards.items():
            if _bc.get("mode") == "serial":
                try:
                    HUB.read_relays(_bn, int(_bc.get("channels", 1)))
                    print(f"[init] 串口继电器板 {_bn} 已认口: {HUB._serial_ports.get(_bn)}", flush=True)
                except Exception as e:
                    print(f"[init] 串口继电器板 {_bn} 未认到（供电9~36V/AB极性/USB？）: {e}", flush=True)
    else:
        transport = make_transport(args)
        LINK = RelayLink(transport, address=args.address)

    httpd = ThreadingHTTPServer((args.http_host, args.http_port), Handler)
    print(f"[http] 监听 {args.http_host}:{args.http_port}"
          f"  (GET /health /relays  POST /relay)", flush=True)
    print("等继电器连云上线后即可控泵。Ctrl+C 停止。", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。", flush=True)
    finally:
        try:
            transport.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
