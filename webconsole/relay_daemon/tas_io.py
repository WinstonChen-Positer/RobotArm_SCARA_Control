"""
tas_io.py — 塔石 TAS-IO-230 系列 (231 / 232 / 233 及 D/R/F/C/L 变体) IO 继电器模块控制库
=========================================================================================

本库把设备说明书 / 寄存器手册 / 《综合指令(参考)1.2》里全部 Modbus 功能，封装成
一组易用、带中文注释的 Python 函数。同一套 API 同时支持：

    * 串口  (RS485 / RS232，Modbus-RTU)        —— SerialTransport
    * 以太网 (TCP，Modbus-RTU-over-TCP 透传)    —— TcpTransport

协议要点（来自官方资料，已在代码中固化）：
    - 默认从站地址：0x11 (十进制 17)
    - 默认串口参数：9600, 8 数据位, 1 停止位, 无校验 (8N1)
    - 继电器 DO   : 线圈地址 0x0000 起；FC01 读，FC05 写单路，FC0F 写多路
    - 开关量 DI   : 离散量地址 0x0020 起；FC02 读
    - 模拟量 AI   : 电压 0x0080 起、电流 0x00C0 起；FC03/04 读
    - 延时/点动   : 保持寄存器 0x9604 起，每路占 4 个寄存器；FC16 写 3 个寄存器
    - 各机型通道数：
        TAS-IO-231 -> DO4 / DI4 / AI0
        TAS-IO-232 -> DO2 / DI2 / AI2
        TAS-IO-233 -> DO2 / DI0 / AI4

作者：调试自动化脚本生成
"""

from __future__ import annotations
import socket
import struct
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

try:
    import serial  # pyserial
except Exception:  # pragma: no cover
    serial = None


# --------------------------------------------------------------------------- #
#  Modbus 工具函数
# --------------------------------------------------------------------------- #
def crc16_modbus(data: bytes) -> bytes:
    """计算 Modbus-RTU CRC-16，返回 2 字节（低字节在前），可直接追加到帧尾。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def build_frame(payload: bytes) -> bytes:
    """payload(地址+功能码+数据) -> 完整 RTU 帧(追加 CRC)。"""
    return payload + crc16_modbus(payload)


def check_frame(resp: bytes) -> bytes:
    """校验响应帧 CRC，正确则返回去掉 CRC 的内容，否则抛异常。"""
    if len(resp) < 4:
        raise ModbusError(f"响应过短: {resp.hex(' ')}")
    body, crc = resp[:-2], resp[-2:]
    if crc16_modbus(body) != crc:
        raise ModbusError(f"CRC 校验失败: {resp.hex(' ')}")
    # Modbus 异常响应：功能码最高位置1
    if len(body) >= 2 and body[1] & 0x80:
        code = body[2] if len(body) > 2 else 0
        raise ModbusError(f"设备返回异常码 0x{code:02X} (功能码 0x{body[1]:02X})")
    return body


class ModbusError(Exception):
    """Modbus 通信 / 协议错误。"""


# --------------------------------------------------------------------------- #
#  传输层
# --------------------------------------------------------------------------- #
class BaseTransport:
    def transact(self, frame: bytes, expect: int = 0) -> bytes:
        raise NotImplementedError
    def close(self) -> None:
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        self.close()


class SerialTransport(BaseTransport):
    """串口(RS485/RS232) Modbus-RTU 传输。"""

    def __init__(self, port: str, baudrate: int = 9600, parity: str = "N",
                 bytesize: int = 8, stopbits: int = 1, timeout: float = 0.4):
        if serial is None:
            raise RuntimeError("需要 pyserial：pip install pyserial")
        self.ser = serial.Serial(port, baudrate, bytesize=bytesize,
                                 parity=parity, stopbits=stopbits, timeout=timeout)

    def transact(self, frame: bytes, expect: int = 0) -> bytes:
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        time.sleep(0.05)
        # 读到“静默”为止
        deadline = time.time() + (self.ser.timeout or 0.4)
        buf = bytearray()
        while time.time() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
                if expect and len(buf) >= expect:
                    break
                deadline = time.time() + 0.05
        return bytes(buf)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class TcpTransport(BaseTransport):
    """以太网 TCP 传输（Modbus-RTU 帧直接透传，含 CRC）。

    适用于设备处于 “TCP 服务端 + Modbus RTU 转 TCP” 模式时，
    PC 作为客户端连接 设备IP:本地端口。
    """

    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)

    def transact(self, frame: bytes, expect: int = 0) -> bytes:
        self.sock.sendall(frame)
        buf = bytearray()
        try:
            while True:
                chunk = self.sock.recv(256)
                if not chunk:
                    break
                buf += chunk
                if expect and len(buf) >= expect:
                    break
                self.sock.settimeout(0.15)
        except socket.timeout:
            pass
        return bytes(buf)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class TcpMbapTransport(BaseTransport):
    """标准 Modbus TCP (MBAP) 传输。

    适用于标准 Modbus TCP 服务端（如 DUCO 机械臂控制器自带 IO，端口 502），
    与 TcpTransport(RTU-over-TCP) 不同：MBAP 帧无 CRC、带事务/协议/长度头。

    上层 TASIO 仍构造 RTU 帧(addr+payload+CRC)；本层剥掉 addr/CRC、套 MBAP 头
    发送，收到 MBAP 响应后再还原成 RTU 帧(unit+payload+CRC)供 check_frame 校验，
    从而让同一套 TASIO API 无缝支持标准 Modbus TCP 设备。

    src_ip：可选，绑定源 IP 强制走指定网卡（多网卡/多网段环境，如设备与 PC 同在
    某张网卡的网段但默认路由走了别的口时）。
    """

    def __init__(self, host: str, port: int = 502, timeout: float = 1.0,
                 src_ip: Optional[str] = None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        if src_ip:
            self.sock.bind((src_ip, 0))
        self.sock.connect((host, port))
        self._tid = 0

    def transact(self, frame: bytes, expect: int = 0) -> bytes:
        # frame = RTU: addr(1) + payload(功能码+数据) + CRC(2)
        addr = frame[0]
        payload = frame[1:-2]
        self._tid = (self._tid + 1) & 0xFFFF
        mbap = struct.pack(">HHH", self._tid, 0, len(payload) + 1) + bytes([addr]) + payload
        self.sock.sendall(mbap)
        hdr = self._recv_exact(7)               # 事务(2)+协议(2)+长度(2)+单元(1)
        length = struct.unpack(">H", hdr[4:6])[0]
        unit = hdr[6]
        body = self._recv_exact(length - 1)     # length 含 unit，故 PDU 长度为 length-1
        rtu = bytes([unit]) + body
        return rtu + crc16_modbus(rtu)          # 还原 RTU 帧供 check_frame 校验

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ModbusError("MBAP 连接中断")
            buf += chunk
        return bytes(buf)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  机型定义
# --------------------------------------------------------------------------- #
@dataclass
class Model:
    name: str
    do: int   # 继电器路数
    di: int   # 开关量输入路数
    ai: int   # 模拟量输入路数


MODELS = {
    "231": Model("TAS-IO-231", do=4, di=4, ai=0),
    "232": Model("TAS-IO-232", do=2, di=2, ai=2),
    "233": Model("TAS-IO-233", do=2, di=0, ai=4),
}


# --------------------------------------------------------------------------- #
#  主控制类
# --------------------------------------------------------------------------- #
class TASIO:
    """TAS-IO-230 系列设备控制器。

    用法::

        from tas_io import TASIO, SerialTransport, TcpTransport

        # 串口
        dev = TASIO(SerialTransport("COM9", 9600), address=0x11)
        # 或 以太网
        dev = TASIO(TcpTransport("192.168.0.80", 10123), address=0x11)

        dev.relay_on(1)            # 闭合 1 路
        dev.relay_off(1)           # 断开 1 路
        print(dev.read_relays(4))  # 读取 4 路状态
    """

    # 寄存器基址
    DO_COIL_BASE = 0x0000     # 继电器线圈
    DI_BASE      = 0x0020     # 开关量输入
    AI_VOLT_BASE = 0x0080     # 模拟量-电压
    AI_CURR_BASE = 0x00C0     # 模拟量-电流
    DELAY_BASE   = 0x9604     # 延时/点动控制（每路 +4）

    def __init__(self, transport: BaseTransport, address: int = 0x11):
        self.t = transport
        self.addr = address

    # ----- 底层 ----------------------------------------------------------- #
    def _do(self, payload: bytes, expect: int = 0) -> bytes:
        frame = build_frame(bytes([self.addr]) + payload)
        resp = self.t.transact(frame, expect=expect)
        if not resp:
            raise ModbusError("无响应（检查接线 / 地址 / 波特率 / 子网）")
        return check_frame(resp)

    @staticmethod
    def _bits_from_bytes(raw: bytes, count: int) -> List[bool]:
        bits = []
        for i in range(count):
            bits.append(bool(raw[i // 8] >> (i % 8) & 1))
        return bits

    # ----- 继电器 DO ------------------------------------------------------ #
    def relay_on(self, ch: int) -> bool:
        """闭合第 ch 路继电器(1 基)。FC05。"""
        return self._write_single_coil(ch, True)

    def relay_off(self, ch: int) -> bool:
        """断开第 ch 路继电器(1 基)。FC05。"""
        return self._write_single_coil(ch, False)

    def set_relay(self, ch: int, on: bool) -> bool:
        return self._write_single_coil(ch, on)

    def _write_single_coil(self, ch: int, on: bool) -> bool:
        addr = self.DO_COIL_BASE + (ch - 1)
        val = 0xFF00 if on else 0x0000
        body = self._do(struct.pack(">BHH", 0x05, addr, val), expect=8)
        # 正常回显请求
        return True

    def set_all_relays(self, states: Sequence[bool]) -> bool:
        """一次写多路继电器。states=[True,False,...]，第 0 个对应第 1 路。FC0F。"""
        count = len(states)
        mask = 0
        for i, s in enumerate(states):
            if s:
                mask |= (1 << i)
        nbytes = (count + 7) // 8
        data = mask.to_bytes(nbytes, "little")
        payload = struct.pack(">BHHB", 0x0F, self.DO_COIL_BASE, count, nbytes) + data
        self._do(payload, expect=8)
        return True

    def all_on(self, count: int) -> bool:
        return self.set_all_relays([True] * count)

    def all_off(self, count: int) -> bool:
        return self.set_all_relays([False] * count)

    def read_relays(self, count: int) -> List[bool]:
        """读取 count 路继电器状态(FC01)。返回布尔列表。"""
        body = self._do(struct.pack(">BHH", 0x01, self.DO_COIL_BASE, count),
                        expect=5 + (count + 7) // 8)
        nbytes = body[2]
        return self._bits_from_bytes(body[3:3 + nbytes], count)

    def read_relay(self, ch: int) -> bool:
        return self.read_relays(ch)[ch - 1]

    # ----- 开关量 DI ------------------------------------------------------ #
    def read_inputs(self, count: int) -> List[bool]:
        """读取 count 路开关量输入(FC02)。返回布尔列表。"""
        body = self._do(struct.pack(">BHH", 0x02, self.DI_BASE, count),
                        expect=5 + (count + 7) // 8)
        nbytes = body[2]
        return self._bits_from_bytes(body[3:3 + nbytes], count)

    def read_input(self, ch: int) -> bool:
        return self.read_inputs(ch)[ch - 1]

    # ----- 模拟量 AI ------------------------------------------------------ #
    def read_voltages(self, count: int) -> List[float]:
        """读取 count 路电压(FC03)，单位 V（寄存器为 mV，自动 /1000）。"""
        body = self._do(struct.pack(">BHH", 0x03, self.AI_VOLT_BASE, count),
                        expect=5 + 2 * count)
        nbytes = body[2]
        regs = struct.unpack(">" + "H" * (nbytes // 2), body[3:3 + nbytes])
        return [r / 1000.0 for r in regs]

    def read_currents(self, count: int) -> List[int]:
        """读取 count 路电流(FC03)，返回寄存器原始值（单位见规格书，通常 mA）。"""
        body = self._do(struct.pack(">BHH", 0x03, self.AI_CURR_BASE, count),
                        expect=5 + 2 * count)
        nbytes = body[2]
        return list(struct.unpack(">" + "H" * (nbytes // 2), body[3:3 + nbytes]))

    # ----- 延时 / 点动 ---------------------------------------------------- #
    def delay_control(self, ch: int, mode: int, close: bool, seconds: int) -> bool:
        """延时 / 点动控制(FC16)。

        ch      : 路号(1 基)
        mode    : 1=普通模式(到时动作并保持)  2=点动模式(动作后自动恢复)
        close   : True=闭合/点动开  False=断开/点动关
        seconds : 1-65535 秒
        """
        addr = self.DELAY_BASE + 4 * (ch - 1)
        action = 0xFF00 if close else 0x0000
        # 帧头(寄存器地址/数量/字节数)为标准大端；
        # 但这 3 个数据寄存器(控制模式/输出动作/延时时间)按官方指令为“低字节在前”：
        #   普通闭合2S -> 01 00 | 00 FF | 02 00
        data = struct.pack("<HHH", mode, action, seconds)
        payload = struct.pack(">BHHB", 0x10, addr, 3, 6) + data
        self._do(payload, expect=8)
        return True

    def delay_close(self, ch: int, seconds: int) -> bool:
        """普通模式：延时 seconds 秒后闭合并保持。"""
        return self.delay_control(ch, mode=1, close=True, seconds=seconds)

    def delay_open(self, ch: int, seconds: int) -> bool:
        """普通模式：延时 seconds 秒后断开。"""
        return self.delay_control(ch, mode=1, close=False, seconds=seconds)

    def jog(self, ch: int, seconds: int) -> bool:
        """点动模式：立即闭合，seconds 秒后自动断开。"""
        return self.delay_control(ch, mode=2, close=True, seconds=seconds)

    # ----- 配置 / 信息 ---------------------------------------------------- #
    def read_serial_settings(self) -> bytes:
        """读取 FLASH 串口设置寄存器 0x0300(4 字节)。"""
        body = self._do(struct.pack(">BHH", 0x03, 0x0300, 0x0002), expect=9)
        return body[3:3 + body[2]]

    def read_modbus_address(self) -> int:
        """读取设备 Modbus 地址寄存器 0x0320。"""
        body = self._do(struct.pack(">BHH", 0x03, 0x0320, 0x0001), expect=7)
        return struct.unpack(">H", body[3:5])[0]

    # ----- 通用 Modbus 原语（可访问任意寄存器/线圈，覆盖全部功能） --------- #
    def read_coils(self, addr: int, count: int) -> List[bool]:
        """FC01 读线圈(继电器/输出类)。"""
        body = self._do(struct.pack(">BHH", 0x01, addr, count), expect=5 + (count + 7)//8)
        return self._bits_from_bytes(body[3:3 + body[2]], count)

    def read_discrete_inputs(self, addr: int, count: int) -> List[bool]:
        """FC02 读离散输入(DI/按键类)。"""
        body = self._do(struct.pack(">BHH", 0x02, addr, count), expect=5 + (count + 7)//8)
        return self._bits_from_bytes(body[3:3 + body[2]], count)

    def read_holding_registers(self, addr: int, count: int) -> List[int]:
        """FC03 读保持寄存器，返回无符号 16 位整数列表。"""
        body = self._do(struct.pack(">BHH", 0x03, addr, count), expect=5 + 2 * count)
        n = body[2]
        return list(struct.unpack(">" + "H" * (n // 2), body[3:3 + n]))

    def read_input_registers(self, addr: int, count: int) -> List[int]:
        """FC04 读输入寄存器。"""
        body = self._do(struct.pack(">BHH", 0x04, addr, count), expect=5 + 2 * count)
        n = body[2]
        return list(struct.unpack(">" + "H" * (n // 2), body[3:3 + n]))

    def read_registers_raw(self, addr: int, count: int) -> bytes:
        """FC03 读保持寄存器，返回原始字节(用于字符串类寄存器，如设备名/MAC)。"""
        body = self._do(struct.pack(">BHH", 0x03, addr, count), expect=5 + 2 * count)
        return body[3:3 + body[2]]

    def write_coil(self, addr: int, on: bool) -> bool:
        """FC05 写单个线圈。"""
        self._do(struct.pack(">BHH", 0x05, addr, 0xFF00 if on else 0x0000), expect=8)
        return True

    def write_register(self, addr: int, value: int) -> bool:
        """FC06 写单个保持寄存器。"""
        self._do(struct.pack(">BHH", 0x06, addr, value & 0xFFFF), expect=8)
        return True

    def write_coils(self, addr: int, bits: Sequence[bool]) -> bool:
        """FC0F 写多个线圈。"""
        count = len(bits)
        mask = 0
        for i, b in enumerate(bits):
            if b:
                mask |= (1 << i)
        nbytes = (count + 7) // 8
        payload = struct.pack(">BHHB", 0x0F, addr, count, nbytes) + mask.to_bytes(nbytes, "little")
        self._do(payload, expect=8)
        return True

    def write_registers(self, addr: int, values: Sequence[int]) -> bool:
        """FC16 写多个保持寄存器(大端)。"""
        data = b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
        payload = struct.pack(">BHHB", 0x10, addr, len(values), len(data)) + data
        self._do(payload, expect=8)
        return True

    # ----- 设备信息（已在本机实测可读） --------------------------------- #
    def read_device_name(self) -> str:
        """读设备名称寄存器 0x1200(如 'IO-240R-S2')。"""
        return self.read_registers_raw(0x1200, 30).split(b"\x00")[0].decode("ascii", "replace").strip()

    def read_firmware_info(self) -> str:
        """读单片机编译信息寄存器 0x1380(含固件版本/编译日期)。"""
        return self.read_registers_raw(0x1380, 30).split(b"\x00")[0].decode("ascii", "replace").strip()

    def read_mcu_sw_version(self) -> str:
        """读 MCU 软件版本寄存器 0x0360。"""
        return self.read_registers_raw(0x0360, 4).split(b"\x00")[0].decode("ascii", "replace").strip()

    def read_mcu_hw_version(self) -> str:
        """读 MCU 硬件版本寄存器 0x0370。"""
        return self.read_registers_raw(0x0370, 4).split(b"\x00")[0].decode("ascii", "replace").strip()

    # ----- 其它输入类(已在本机实测可读) -------------------------------- #
    def read_pulse_counts(self, count: int) -> List[int]:
        """读脉冲计数寄存器 0x0060 起(DI 上升沿计数)。"""
        return self.read_holding_registers(0x0060, count)

    def read_offline_status(self) -> int:
        """读离线状态寄存器 0x00C8。"""
        return self.read_holding_registers(0x00C8, 1)[0]

    def set_modbus_address(self, new_addr: int) -> bool:
        """写 Modbus 从站地址寄存器 0x0320(1~253)。⚠改后需用新地址通信。"""
        return self.write_registers(0x0320, [new_addr])

    def save_params(self) -> bool:
        """保存参数到 FLASH(保存模式寄存器 0x0380 写 1=全部保存)。"""
        return self.write_registers(0x0380, [0x0001])

    # ----- 机型识别 ------------------------------------------------------- #
    def detect_model(self) -> Optional[Model]:
        """通过尝试读取 DO/DI/AI 通道，推断设备机型(231/232/233)。"""
        def ok_read(fn):
            try:
                fn(); return True
            except Exception:
                return False
        di4 = ok_read(lambda: self.read_inputs(4))
        ai4 = ok_read(lambda: self.read_voltages(4))
        if di4 and not ai4:
            return MODELS["231"]
        if ai4 and not di4:
            return MODELS["233"]
        return MODELS["232"]


if __name__ == "__main__":
    # 简单自检：仅演示用法（需真实连接）
    import sys
    print(__doc__)
    print("CRC 自检 11 05 00 00 FF 00 ->",
          build_frame(bytes.fromhex("1105000 0FF00".replace(' ',''))).hex(' '))
