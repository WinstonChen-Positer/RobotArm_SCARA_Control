"""
tas_io_ethernet.py — 以太网"云透传"通道传输层
=================================================

适用场景：TAS-IO-230R 系列以太网口被配置为 **TCP 客户端**，主动连接塔石云服务器
(本例为 47.99.152.116:10067)。设备处于 DHCP 模式。

本模块让 PC 冒充该云服务器：
  1. 起一个最小 DHCP 服务，给设备下发租约（设备拿到 IP 后才会发起连接）；
  2. 在本机监听云端口，接受设备连接；
  3. 设备上线后先发一个注册包(其 MAC 的 ASCII)，随后这条 TCP 即为 Modbus-RTU 透传通道；
  4. 在该通道上收发标准 Modbus-RTU 帧即可读写继电器/输入/模拟量。

依赖：scapy + npcap（用于 DHCP）。需先把本机网卡配到设备网段，并绑定云目标 IP：
    New-NetIPAddress -InterfaceIndex <idx> -IPAddress 192.168.0.1    -PrefixLength 24
    New-NetIPAddress -InterfaceIndex <idx> -IPAddress 47.99.152.116  -PrefixLength 32
（以上两条需管理员权限；其中 192.168.0.1 = 设备网关，47.99.152.116 = 设备目标云 IP）

用法：
    from tas_io import TASIO
    from tas_io_ethernet import CloudServerTransport, find_nic_mac

    tr = CloudServerTransport(
            listen_port=10067,
            device_mac="3c:ab:72:02:93:76",
            offer_ip="192.168.0.80",
            server_ip="192.168.0.1",
            nic_mac="04:7c:16:70:a8:e9")
    tr.wait_for_device(timeout=90)         # 等设备上线
    dev = TASIO(tr, address=0x11)
    print(dev.read_relays(4))
    dev.relay_on(1)
"""
from __future__ import annotations
import socket
import threading
import time

from tas_io import crc16_modbus, build_frame, BaseTransport, ModbusError

try:
    import serial  # pyserial（第三板串口/485 用；网络板不需要）
except Exception:
    serial = None

try:
    from scapy.all import (get_working_ifaces, sniff, sendp, Ether, IP, UDP,
                           BOOTP, DHCP, conf)
    conf.verb = 0
    _HAS_SCAPY = True
except Exception:
    _HAS_SCAPY = False


def find_nic_mac(description_substr: str = "I225") -> str | None:
    """按网卡描述关键字找到本机有线网卡 MAC。"""
    if not _HAS_SCAPY:
        return None
    for ifc in get_working_ifaces():
        if description_substr.lower() in (getattr(ifc, "description", "") or "").lower():
            return getattr(ifc, "mac", None)
    return None


def _scan_modbus_reply(buf: bytes):
    """在字节流中查找一个 CRC 合法、以指定地址开头的 Modbus 响应，返回 (起点, 帧)。"""
    for i in range(len(buf)):
        for ln in range(5, min(len(buf) - i, 260) + 1):
            frag = buf[i:i + ln]
            if len(frag) >= 4 and crc16_modbus(frag[:-2]) == frag[-2:] and frag[1] in (
                    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x0F, 0x10, 0x81, 0x82, 0x83,
                    0x84, 0x85, 0x86, 0x8F, 0x90):
                return i, frag
    return None, None


class CloudServerTransport(BaseTransport):
    """冒充云服务器，接受设备 TCP 连接，提供 Modbus 透传。"""

    def __init__(self, listen_port: int, device_mac: str, offer_ip: str,
                 server_ip: str, nic_mac: str, lease: int = 86400):
        if not _HAS_SCAPY:
            raise RuntimeError("需要 scapy + npcap 才能运行 DHCP 服务")
        self.listen_port = listen_port
        self.device_mac = device_mac.lower()
        self.offer_ip = offer_ip
        self.server_ip = server_ip
        self.lease = lease
        self._iface = next(i for i in get_working_ifaces()
                           if (getattr(i, "mac", "") or "").lower() == nic_mac.lower())
        self._nic_mac = nic_mac
        self._buf = bytearray()
        self._conn = None
        self._stop = False
        self.registration = None
        threading.Thread(target=self._dhcp_loop, daemon=True).start()

    # ---- DHCP 服务 ----
    def _opts(self, mt):
        return [("message-type", mt), ("server_id", self.server_ip),
                ("lease_time", self.lease), ("subnet_mask", "255.255.255.0"),
                ("router", self.server_ip), ("broadcast_address", "192.168.0.255"),
                ("name_server", self.server_ip), "end"]

    def _dhcp_handle(self, p):
        if not (p.haslayer(BOOTP) and p.haslayer(DHCP)):
            return
        if p[Ether].src.lower() != self.device_mac:
            return
        mt = [o[1] for o in p[DHCP].options if isinstance(o, tuple) and o[0] == "message-type"]
        if not mt:
            return
        b = p[BOOTP]
        base = (Ether(src=self._nic_mac, dst=self.device_mac) /
                IP(src=self.server_ip, dst="255.255.255.255") /
                UDP(sport=67, dport=68) /
                BOOTP(op=2, htype=1, hlen=6, xid=b.xid, yiaddr=self.offer_ip,
                      siaddr=self.server_ip, chaddr=b.chaddr))
        if mt[0] == 1:
            sendp(base / DHCP(options=self._opts("offer")), iface=self._iface, verbose=0)
        elif mt[0] == 3:
            sendp(base / DHCP(options=self._opts("ack")), iface=self._iface, verbose=0)

    def _dhcp_loop(self):
        sniff(iface=self._iface, prn=self._dhcp_handle,
              stop_filter=lambda p: self._stop, timeout=3600, store=False)

    # ---- 等待设备连接 ----
    def wait_for_device(self, timeout: float = 90):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.listen_port))
        srv.listen(1)
        srv.settimeout(timeout)
        self._conn, peer = srv.accept()
        srv.close()
        self._conn.settimeout(2.0)
        # 读取注册包(MAC 的 ASCII)
        try:
            self.registration = self._conn.recv(64)
        except socket.timeout:
            self.registration = b""
        return peer

    # ---- Modbus 透传 ----
    def transact(self, frame: bytes, expect: int = 0) -> bytes:
        if self._conn is None:
            raise ModbusError("设备尚未连接，请先调用 wait_for_device()")
        self._conn.sendall(frame)
        end = time.time() + 2.0
        self._conn.settimeout(0.3)
        while time.time() < end:
            try:
                d = self._conn.recv(256)
                if not d:
                    break
                self._buf += d
            except socket.timeout:
                pass
            idx, reply = _scan_modbus_reply(bytes(self._buf))
            if reply is not None:
                del self._buf[:idx + len(reply)]
                return reply
        return b""

    def reset_conn(self):
        """只关闭当前 TCP 连接，保留 DHCP（scapy 线程）。逼继电器重连后 wait_for_device 重新 accept。"""
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        self._buf = bytearray()

    def close(self):
        self._stop = True
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass


# ============================================================
# 多板并发（单机迁移 2026-07-16）：同一云端口(10067)同时接住多块继电器板，
# 按对端 IP 区分连接。生产需同时控 old(大泵/排气阀) + new(吸盘泵/各阀) 两块板，
# 而两块板都拨向 47.99.152.116:10067，原单连接 CloudServerTransport 一次只连一块。
# ============================================================

class _BoardChannel(BaseTransport):
    """把某块板(peer_ip)包装成 TASIO 可直接使用的单板传输视图。"""

    def __init__(self, multi: "MultiCloudServerTransport", peer_ip: str):
        self._multi = multi
        self._peer_ip = peer_ip

    def transact(self, frame: bytes, expect: int = 0) -> bytes:
        return self._multi.transact(frame, self._peer_ip, expect)


class MultiCloudServerTransport:
    """冒充云服务器，同一监听端口上并发接住多块继电器板（按 peer IP 索引）。

    - DHCP：对 dhcp_offers 里每个 {mac: offer_ip} 应答（已有静态 IP 的板可不列）。
    - TCP：后台 accept_loop 持续 accept，按对端 IP 存 socket + 独立收发缓冲。
    - transact(frame, peer_ip)：在该板连接上跑一次 Modbus-RTU 事务，线程安全。
    """

    def __init__(self, listen_port: int, dhcp_offers: dict, server_ip: str,
                 nic_mac: str, lease: int = 86400):
        if not _HAS_SCAPY:
            raise RuntimeError("需要 scapy + npcap 才能运行 DHCP 服务")
        self.listen_port = listen_port
        self.dhcp_offers = {k.lower(): v for k, v in (dhcp_offers or {}).items()}
        self.server_ip = server_ip
        self.lease = lease
        self._iface = next(i for i in get_working_ifaces()
                           if (getattr(i, "mac", "") or "").lower() == nic_mac.lower())
        self._nic_mac = nic_mac
        self._conns: dict = {}     # peer_ip -> socket
        self._bufs: dict = {}      # peer_ip -> bytearray
        self._lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._dhcp_loop, daemon=True).start()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # ---- DHCP ----
    def _opts(self, mt):
        return [("message-type", mt), ("server_id", self.server_ip),
                ("lease_time", self.lease), ("subnet_mask", "255.255.255.0"),
                ("router", self.server_ip), ("broadcast_address", "192.168.0.255"),
                ("name_server", self.server_ip), "end"]

    def _dhcp_handle(self, p):
        if not (p.haslayer(BOOTP) and p.haslayer(DHCP)):
            return
        src = p[Ether].src.lower()
        offer_ip = self.dhcp_offers.get(src)
        if not offer_ip:
            return
        mt = [o[1] for o in p[DHCP].options if isinstance(o, tuple) and o[0] == "message-type"]
        if not mt:
            return
        b = p[BOOTP]
        base = (Ether(src=self._nic_mac, dst=src) /
                IP(src=self.server_ip, dst="255.255.255.255") /
                UDP(sport=67, dport=68) /
                BOOTP(op=2, htype=1, hlen=6, xid=b.xid, yiaddr=offer_ip,
                      siaddr=self.server_ip, chaddr=b.chaddr))
        if mt[0] == 1:
            sendp(base / DHCP(options=self._opts("offer")), iface=self._iface, verbose=0)
        elif mt[0] == 3:
            sendp(base / DHCP(options=self._opts("ack")), iface=self._iface, verbose=0)

    def _dhcp_loop(self):
        sniff(iface=self._iface, prn=self._dhcp_handle,
              stop_filter=lambda p: self._stop, timeout=3600, store=False)

    # ---- 多连接 accept ----
    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.listen_port))
        srv.listen(8)
        srv.settimeout(1.0)
        while not self._stop:
            try:
                conn, peer = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            ip = peer[0]
            conn.settimeout(2.0)
            try:
                reg = conn.recv(64)   # 注册包(MAC ASCII)
            except Exception:
                reg = b""
            with self._lock:
                old = self._conns.get(ip)
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
                self._conns[ip] = conn
                self._bufs[ip] = bytearray()
            print(f"[link] 板已连接: {peer} reg={reg!r}", flush=True)
        try:
            srv.close()
        except Exception:
            pass

    def peers(self):
        with self._lock:
            return list(self._conns.keys())

    def _drop(self, ip):
        with self._lock:
            c = self._conns.pop(ip, None)
            self._bufs.pop(ip, None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def transact(self, frame: bytes, peer_ip: str, expect: int = 0) -> bytes:
        with self._lock:
            conn = self._conns.get(peer_ip)
        if conn is None:
            raise ModbusError(f"板 {peer_ip} 未连接")
        try:
            conn.sendall(frame)
        except (OSError, ConnectionError) as e:
            self._drop(peer_ip)
            raise ModbusError(f"板 {peer_ip} socket 断开: {e}")
        end = time.time() + 2.0
        conn.settimeout(0.3)
        while time.time() < end:
            try:
                d = conn.recv(256)
                if not d:
                    break
                with self._lock:
                    buf = self._bufs.get(peer_ip)
                    if buf is None:
                        raise ModbusError(f"板 {peer_ip} 已掉线")
                    buf += d
            except socket.timeout:
                pass
            except (OSError, ConnectionError) as e:
                self._drop(peer_ip)
                raise ModbusError(f"板 {peer_ip} 读断开: {e}")
            with self._lock:
                buf = self._bufs.get(peer_ip)
                data = bytes(buf) if buf is not None else b""
            idx, reply = _scan_modbus_reply(data)
            if reply is not None:
                with self._lock:
                    buf = self._bufs.get(peer_ip)
                    if buf is not None:
                        del buf[:idx + len(reply)]
                return reply
        return b""

    def board_channel(self, peer_ip: str) -> "_BoardChannel":
        return _BoardChannel(self, peer_ip)

    def close(self):
        self._stop = True
        with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
            self._bufs.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass


def tcp_reachable(ip: str, port: int, timeout: float = 0.6) -> bool:
    """快速 TCP 连通性探测（用于 /health 判断 client-server 板是否在线）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


class ClientBoardTransport(BaseTransport):
    """主动连接到 TCP-Server 模式的板子（如 old 板 192.168.0.80:10123），Modbus-RTU 透传。

    与 CloudServerTransport 相反：这里 PC 是 TCP 客户端、板子是服务器。断线在下次
    transact 时自动重连。线程安全（单锁串行化一次事务）。不需要 npcap/管理员。
    """

    def __init__(self, ip: str, port: int, connect_timeout: float = 3.0,
                 io_timeout: float = 0.3):
        self.ip = ip
        self.port = int(port)
        self._ct = connect_timeout
        self._iot = io_timeout
        self._s = None
        self._buf = bytearray()
        self._lock = threading.Lock()

    def _ensure(self):
        if self._s is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self._ct)
        s.connect((self.ip, self.port))
        s.settimeout(self._iot)
        self._s = s
        self._buf = bytearray()

    def _drop(self):
        try:
            if self._s:
                self._s.close()
        except Exception:
            pass
        self._s = None
        self._buf = bytearray()

    def transact(self, frame: bytes, expect: int = 0) -> bytes:
        with self._lock:
            self._ensure()
            try:
                self._s.sendall(frame)
            except (OSError, ConnectionError) as e:
                self._drop()
                raise ModbusError(f"client板 {self.ip}:{self.port} 发送失败: {e}")
            end = time.time() + 2.0
            while time.time() < end:
                try:
                    d = self._s.recv(256)
                    if not d:
                        break
                    self._buf += d
                except socket.timeout:
                    pass
                except (OSError, ConnectionError) as e:
                    self._drop()
                    raise ModbusError(f"client板 {self.ip}:{self.port} 读失败: {e}")
                idx, reply = _scan_modbus_reply(bytes(self._buf))
                if reply is not None:
                    del self._buf[:idx + len(reply)]
                    return reply
            return b""

    def close(self):
        with self._lock:
            self._drop()


class SerialBoardTransport(BaseTransport):
    """串口(RS485) Modbus-RTU 板（塔石 TAS-IO 第三板，经 485转USB 上机）。

    与 ClientBoardTransport 同构（同满足 BaseTransport.transact 契约），只是底层
    是 pyserial 而非 TCP：首个 transact 时开口(懒开)，串口异常时 drop、下次自动重开，
    单锁串行化一次事务——正好契合 RelayHub._op 的重试/reset 模型。不需 npcap/管理员。
    读到「静默」为止（同 tas_io.SerialTransport 的读法），够 expect 字节即提前返回。
    """

    def __init__(self, port: str, baud: int = 9600, parity: str = "N",
                 bytesize: int = 8, stopbits: int = 1, timeout: float = 0.4):
        if serial is None:
            raise RuntimeError("需要 pyserial：pip install pyserial")
        self.port = port
        self.baud = int(baud)
        self._parity = parity
        self._bytesize = int(bytesize)
        self._stopbits = int(stopbits)
        self._timeout = float(timeout)
        self._ser = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._ser is not None and self._ser.is_open:
            return
        self._ser = serial.Serial(self.port, self.baud, bytesize=self._bytesize,
                                  parity=self._parity, stopbits=self._stopbits,
                                  timeout=self._timeout)

    def _drop(self):
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    def transact(self, frame: bytes, expect: int = 0) -> bytes:
        with self._lock:
            try:
                self._ensure()
            except Exception as e:
                self._drop()
                raise ModbusError(f"串口板 {self.port} 打开失败: {e}")
            try:
                self._ser.reset_input_buffer()
                self._ser.write(frame)
                self._ser.flush()
            except (OSError, serial.SerialException) as e:
                self._drop()
                raise ModbusError(f"串口板 {self.port} 发送失败: {e}")
            time.sleep(0.05)
            deadline = time.time() + (self._timeout or 0.4)
            buf = bytearray()
            while time.time() < deadline:
                try:
                    chunk = self._ser.read(256)
                except (OSError, serial.SerialException) as e:
                    self._drop()
                    raise ModbusError(f"串口板 {self.port} 读失败: {e}")
                if chunk:
                    buf += chunk
                    if expect and len(buf) >= expect:
                        break
                    deadline = time.time() + 0.05
            return bytes(buf)

    def close(self):
        with self._lock:
            self._drop()


def find_serial_board_port(baud: int = 9600, addr: int = 0x11,
                           channels: int = 1) -> str | None:
    """扫 CH340 串口找会应答的塔石 TAS-IO 板（FC01 读线圈站号 addr）。

    第三板经 485转USB 上机、口号会随重插漂移，故启动时自动认口：逐个 CH340 口
    发一帧 FC01 读 channels 路线圈，能拿到 CRC 合法、地址匹配的响应即认定是它。
    找不到返回 None（板子没接通/掉电时优雅降级，不崩）。
    """
    if serial is None:
        return None
    try:
        from serial.tools import list_ports
    except Exception:
        return None
    req = build_frame(bytes([addr, 0x01, 0x00, 0x00, (channels >> 8) & 0xFF, channels & 0xFF]))
    for p in list_ports.comports():
        desc = "%s %s" % (p.description or "", p.hwid or "")
        if "CH340" not in desc.upper() and "1A86" not in desc.upper():
            continue
        try:
            tr = SerialBoardTransport(p.device, baud=baud)
            resp = tr.transact(req, expect=5 + (channels + 7) // 8)
            tr.close()
        except Exception:
            continue
        if len(resp) >= 5 and resp[0] == addr and resp[1] == 0x01 \
                and crc16_modbus(resp[:-2]) == resp[-2:]:
            return p.device
    return None
