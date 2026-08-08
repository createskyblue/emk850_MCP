#!/usr/bin/env python3
"""EMK850+ 低功耗分析仪串口协议编解码 —— 两段式解析架构。

帧格式（固定 64 字节）:
    [0]  magic 0x33
    [1]  cmd
    [2]  len    (payload 有效长度, 0~60)
    [3]  seq    (普通帧固定 0x00; 大块数据延续帧是帧序号 1,2,3...)
    [4..63] payload (60 字节, 不足补 0)

两段式架构:
    第一段 FrameDecoder : 逐字节流式状态机, 字节流 -> 数据帧, 产出推入队列
        - 任何一字节匹配/校验失败, 只丢弃"当前这一字节", 剩余字节继续匹配
        - 只校验 magic + len, 不做命令级解析, 保证通用
    第二段 ProtocolParser: 从队列消费数据帧, 按 cmd 分发到各处理器
        - 版本 / 配置块重组 / 采样解算 / 清零确认等
        - 永远不接触原始字节流
两段之间用 queue.Queue 解耦, 第一段只管生产, 第二段只管消费。
"""
from __future__ import annotations

import math
import queue
import struct

MAGIC = 0x33
FRAME_SIZE = 64
PAYLOAD_SIZE = 60

# ---- 命令定义 (mpa/Protocol.cs) ----
CMD_REQ_VERSION = 0x10          # 16  请求版本
CMD_RES_VERSION = 0x11          # 17  版本响应
CMD_REQ_POWERON = 0x12          # 18  电源开
CMD_RES_POWERON = 0x13          # 19
CMD_REQ_POWEROFF = 0x14         # 20  电源关
CMD_RES_POWEROFF = 0x15         # 21
CMD_REQ_START = 0x16            # 22  启动采样(波形模式)
CMD_REQ_STOP = 0x18             # 24  停止采样(波形模式)
CMD_RES_STOP = 0x19             # 25
CMD_RESULT = 0x21               # 33  采样数据帧(14×Sample)
CMD_SYNC = 0x23                 # 35
CMD_REQ_WRITE_CONFIG = 0x30     # 48  写配置
CMD_RES_WRITE_CONFIG = 0x31     # 49
CMD_REQ_READ_CONFIG = 0x32      # 50  请求读配置
CMD_RES_READ_CONFIG = 0x33      # 51
CMD_REQ_BIG_DATA = 0x40         # 64  大块数据延续帧
CMD_REW_BIG_DATA = 0x41         # 65
CMD_REQ_BIG_DATA_FIRST = 0x42   # 66  大块数据首帧
CMD_REQ_BIG_DATA_LAST = 0x44    # 68
CMD_USER_STOP = 0x5F            # 95  停止(工厂模式)
CMD_USER_START = 0x60           # 96  启动(工厂模式)
CMD_USER_CONFIG_VOLT = 0x61     # 97  设置电压
CMD_USER_CURRENT_RESULT = 0x62  # 98  电流数据帧(工厂)
CMD_USER_VOLT_RESULT = 0x63     # 99  电压数据帧(工厂)
CMD_USER_START_CLEAR = 0x64     # 100 清零开始(空载清零)
CMD_USER_END_CLEAR = 0x65       # 101 清零结束
CMD_REQ_READ_ALARMCONFIG = 0x68  # 104
CMD_REQ_VOLTSET = 0x69          # 105
CMD_AUTOCHECK = 0x74            # 116
CMD_REQ_ID_PRIVATE = 0x75       # 117
CMD_REQ_START_MAX_CURRENT_CHECK = 0x77  # 119
CMD_REQ_STOP_MAX_CURRENT_CHECK = 0x78   # 120
CMD_FORCE_HALL_SENSOR = 0x79    # 121
CMD_CLOSE_HALL_SENSOR = 0x80    # 128
CMD_RESULT_PROCESS = 0x81       # 129
CMD_REQ_100K = 0x82             # 130
CMD_REQ_10K = 0x83              # 131
CMD_REQ_HIGH_SPEED_DATA = 0x84  # 132 高速采样数据帧
CMD_STORE_100K_PARAM_PRIVATE = 0x85  # 133
CMD_REQ_100K_PARAM_PRIVATE = 0x86    # 134
CMD_LCD_ACK = 0xB7              # 183

CMD_NAMES = {
    0x10: "REQ_VERSION", 0x11: "RES_VERSION",
    0x12: "REQ_POWERON", 0x13: "RES_POWERON",
    0x14: "REQ_POWEROFF", 0x15: "RES_POWEROFF",
    0x16: "REQ_START", 0x18: "REQ_STOP", 0x19: "RES_STOP",
    0x21: "RESULT(sample)", 0x23: "SYNC",
    0x30: "REQ_WRITE_CONFIG", 0x31: "RES_WRITE_CONFIG",
    0x32: "REQ_READ_CONFIG", 0x33: "RES_READ_CONFIG",
    0x40: "REQ_BIG_DATA", 0x41: "REW_BIG_DATA",
    0x42: "REQ_BIG_DATA_FIRST", 0x44: "REQ_BIG_DATA_LAST",
    0x5F: "USER_STOP", 0x60: "USER_START",
    0x61: "USER_CONFIG_VOLT", 0x62: "USER_CURRENT_RESULT",
    0x63: "USER_VOLT_RESULT", 0x64: "USER_START_CLEAR",
    0x65: "USER_END_CLEAR", 0x68: "REQ_READ_ALARMCONFIG",
    0x69: "REQ_VOLTSET", 0x74: "AUTOCHECK", 0x75: "REQ_ID_PRIVATE",
    0x77: "START_MAX_CUR", 0x78: "STOP_MAX_CUR",
    0x79: "FORCE_HALL", 0x80: "CLOSE_HALL",
    0x81: "RESULT_PROCESS", 0x82: "REQ_100K", 0x83: "REQ_10K",
    0x84: "HIGH_SPEED_DATA", 0x85: "STORE_100K_PARAM",
    0x86: "REQ_100K_PARAM", 0xB7: "LCD_ACK",
}


def cmd_name(cmd: int) -> str:
    return CMD_NAMES.get(cmd, f"0x{cmd:02X}")


def build_frame(cmd: int, payload: bytes = b"", seq: int = 0) -> bytes:
    """构造 64 字节帧。payload 不超过 60 字节。"""
    assert len(payload) <= PAYLOAD_SIZE, f"payload 超长: {len(payload)}"
    p = bytearray(payload) + bytes(PAYLOAD_SIZE - len(payload))
    return bytes([MAGIC, cmd & 0xFF, len(payload) & 0xFF, seq & 0xFF]) + bytes(p)


# ============================================================
# 第一段: 逐字节流式状态机 (字节流 -> 数据帧)
# ============================================================
class FrameDecoder:
    """字节流 -> 64 字节数据帧。

    逐字节扫描; 任何失败只丢当前 1 字节, 剩余字节继续匹配。
    产出的帧推入 out_queue (默认内部队列, 可用 get_frames() 取出)。
    """

    WAIT_MAGIC, HEADER, PAYLOAD = 0, 1, 2

    def __init__(self, out_queue: queue.Queue | None = None):
        self.out_queue = out_queue if out_queue is not None else queue.Queue()
        self.state = self.WAIT_MAGIC
        self.buf = bytearray()          # 当前候选帧缓冲区(最多 64B)
        self.stats = {"dropped": 0, "frames": 0, "bad_header": 0}

    def feed(self, data: bytes) -> int:
        """喂入字节流, 返回本次产出的帧数(帧已入队)。"""
        n0 = self.stats["frames"]
        for b in data:
            self._feed_byte(b)
        return self.stats["frames"] - n0

    def _feed_byte(self, b: int) -> None:
        if self.state == self.WAIT_MAGIC:
            if b == MAGIC:
                self.buf = bytearray([b])
                self.state = self.HEADER
            else:
                self.stats["dropped"] += 1      # 非 magic, 丢弃这一字节
            return

        self.buf.append(b)

        if self.state == self.HEADER:
            if len(self.buf) == 4:
                # magic(0) cmd(1) len(2) seq(3)
                # 只校验 len: 大块数据延续帧 seq 是 1,2,3...
                if self.buf[2] <= PAYLOAD_SIZE:
                    self.state = self.PAYLOAD
                else:
                    self.stats["bad_header"] += 1
                    self.stats["dropped"] += 1
                    self._rescan()
            return

        if self.state == self.PAYLOAD:
            if len(self.buf) == FRAME_SIZE:
                self._emit(bytes(self.buf))
                self.state = self.WAIT_MAGIC
                self.buf = bytearray()

    def _emit(self, frame: bytes) -> None:
        self.stats["frames"] += 1
        self.out_queue.put({
            "cmd": frame[1],
            "len": frame[2],
            "seq": frame[3],
            "raw": frame,
            "payload": frame[4:4 + frame[2]],
            "payload_full": frame[4:],
        })

    def _rescan(self) -> None:
        """HEADER 校验失败: 只丢失败的那个字节, 剩余字节继续匹配。"""
        rest = bytes(self.buf[1:])
        self.state = self.WAIT_MAGIC
        self.buf = bytearray()
        for b in rest:
            self._feed_byte(b)

    def get_frames(self) -> list[dict]:
        """取出队列里累积的所有帧。"""
        out = []
        while True:
            try:
                out.append(self.out_queue.get_nowait())
            except queue.Empty:
                break
        return out


# ============================================================
# 第二段: 配置块重组器 (消费帧 -> 配置数据)
# ============================================================
class BigDataAssembler:
    """大块数据(配置)重组器。

    FIRST 帧 (0x42): payload[0..3]=头, [4..7]=总长N, [8..11]=累加和,
                     [12..59]=首段数据(48B)
    延续帧 (0x40):   payload[0..]=数据块, 逐帧拼接, 累计到 N 字节
    校验: sum(全部数据字节) == 累加和
    """

    PL_CONFIG2_NAMES = ["voltage", "offset", "max", "g1", "om1", "o1", "p1",
                        "g2", "om2", "o2", "p2", "g3", "om3", "o3", "p3",
                        "o4", "p4", "o5", "p5", "ov", "pv",
                        "ch2_min", "ch2_max", "ch4_min", "ch4_max",
                        "ch5_min", "ch5_max",
                        "om4", "om5"]

    def __init__(self):
        self.reset()

    def reset(self):
        self.buf = bytearray()
        self.total = 0
        self.checksum = 0

    def feed_frame(self, frame: dict) -> bytes | None:
        """喂入一帧; 配置块完整时返回 bytes, 否则 None。"""
        cmd = frame["cmd"]
        if cmd == CMD_REQ_BIG_DATA_FIRST:
            self.reset()
            self.total = struct.unpack_from("<I", frame["payload_full"], 4)[0]
            self.checksum = struct.unpack_from("<I", frame["payload_full"], 8)[0]
            self.buf += frame["payload_full"][12:]
        elif cmd == CMD_REQ_BIG_DATA:
            self.buf += frame["payload"]
        else:
            return None
        if len(self.buf) >= self.total:
            data = bytes(self.buf[: self.total])
            if (sum(data) & 0xFFFFFFFF) == self.checksum:
                return data
            return None
        return None

    @staticmethod
    def to_plconfig2(data: bytes) -> dict:
        """把 216/232/244/264 字节配置块解析成命名字典。"""
        n = len(data) // 8
        vals = struct.unpack(f"<{n}d", data[: n * 8])
        names = BigDataAssembler.PL_CONFIG2_NAMES
        return {names[i]: vals[i] for i in range(min(len(names), n))}


# ============================================================
# 第二段: 协议解析器 (消费队列里的帧 -> 按 cmd 分发)
# ============================================================
class ProtocolParser:
    """从帧队列消费数据帧, 按 cmd 分发解析。

    使用方式:
        q = queue.Queue()
        decoder = FrameDecoder(q)          # 第一段, 喂字节流
        parser = ProtocolParser(q)         # 第二段, 消费帧
        parser.on_cmd(CMD_RES_VERSION, handler)
        ...
        decoder.feed(raw_bytes)            # 生产
        parser.run(timeout=0.5)            # 消费一次(非阻塞)
    """

    def __init__(self, in_queue: queue.Queue | None = None):
        self.in_queue = in_queue if in_queue is not None else queue.Queue()
        self._handlers = {}                # cmd -> [handler(frame)]
        self.on_any = None                 # 全帧回调 fn(frame) (赋值使用)
        self.bigdata = BigDataAssembler()  # 内置配置块重组
        self.config = None                 # 解析完成的 PLConfig2
        self.version = None                # 解析完成的版本字符串
        self.on_config = None              # 配置完成回调 fn(config_dict)
        self.on_version = None             # 版本回调 fn(version_str)

    def on_cmd(self, cmd: int, handler) -> None:
        """注册某命令帧的处理函数 fn(frame)。"""
        self._handlers.setdefault(cmd, []).append(handler)

    def run(self, timeout: float = 0.0, max_frames: int | None = None) -> int:
        """从队列消费帧并分发, 返回处理帧数。timeout>0 时阻塞等待。"""
        n = 0
        while True:
            try:
                frame = self.in_queue.get(timeout=timeout)
                timeout = 0.0
            except queue.Empty:
                break
            self.on_frame(frame)
            n += 1
            if max_frames is not None and n >= max_frames:
                break
        return n

    def on_frame(self, frame: dict) -> None:
        """处理单帧: 内置逻辑 + 用户注册处理器。"""
        cmd = frame["cmd"]

        # 内置: 配置块重组
        if cmd in (CMD_REQ_BIG_DATA_FIRST, CMD_REQ_BIG_DATA):
            cfg = self.bigdata.feed_frame(frame)
            if cfg is not None:
                self.config = BigDataAssembler.to_plconfig2(cfg)
                if self.on_config:
                    self.on_config(self.config)

        # 内置: 版本
        if cmd == CMD_RES_VERSION:
            self.version = parse_version(frame["payload"])
            if self.on_version:
                self.on_version(self.version)

        # 用户注册处理器
        for h in self._handlers.get(cmd, []):
            h(frame)
        if self.on_any is not None:
            self.on_any(frame)


# ---------- 采样载荷解析 ----------

def parse_samples_14(payload_full: bytes):
    """cmd 33/99: payload[4..] = 14×Sample{ushort voltage; short current}。"""
    samples = []
    data = payload_full[4:]
    for i in range(0, min(len(data), 14 * 4), 4):
        voltage, current = struct.unpack_from("<Hh", data, i)
        samples.append((voltage, current))
    return samples


def parse_high_speed(payload_full: bytes):
    """cmd 0x84 高速帧: payload[0..8]=9B通道头, [9..58]=25×short 采样,
    payload[59]=标志。
    返回 (header9, [25 个 current 原始值], flag)。
    约定: 采样[0] 是电压, 采样[1..24] 是电流。"""
    hdr = payload_full[0:9]
    samples = struct.unpack(f"<{25}h", payload_full[9:59])
    flag = payload_full[59]
    return hdr, list(samples), flag


def ch_number(hdr9: bytes, sample_index: int) -> int:
    """9 字节头中每 3 位表示一个采样通道号 (getChNumberToServer)。"""
    idx = sample_index * 3
    byte_pos, bit_pos = idx // 8, idx % 8
    v = 0
    for k in range(3):
        bpos, b = (byte_pos + (bit_pos + k) // 8, (bit_pos + k) % 8)
        v |= ((hdr9[bpos] >> b) & 1) << k
    return v


def parse_sample_current(payload_full: bytes):
    """cmd 98: payload[4..] = 14×SampleCurrent{int current}。"""
    out = []
    data = payload_full[4:]
    for i in range(0, min(len(data), 14 * 4), 4):
        (current,) = struct.unpack_from("<i", data, i)
        out.append(current)
    return out


def parse_version(payload: bytes) -> str:
    """cmd 17 版本响应: 以空终止符结尾的 ASCII 字符串。"""
    return payload.split(b"\x00")[0].decode("ascii", errors="replace").strip()


# ---------- 电流/电压/功耗换算 (HandleSampleHighSpeed, 非5A分支) ----------

# 高速帧: 通道号 -> (om, p, o) 校准系数组
# 实测修正: 厂商 HandleSampleHighSpeed 的 channel 3/4 用 m_conf2_Channel5.om4/om5
# (即配置块 232B 的索引 [27]/[28]), 不是 m_conf2.ch4/ch5。
# 档位: channel0=uA(om1) ch1=nA(om2) ch2=om3(读 µA!) ch3=mA(om4=0.5) ch4=om5
_HS_CH = {
    0: ("om1", "p1", "o1"),   # uA 档 (om1)
    1: ("om2", "p2", "o2"),   # nA 档 (om2)
    2: ("om3", "p3", "o3"),   # om3 档 (实测读 µA 量级)
    3: ("om4", "p4", "o4"),   # mA 档 (om4, 实测 T2=0.5)
    4: ("om5", "p5", "o5"),   # 5 通道 (om5)
}


def current_from_hs(raw: int, channel: int, cfg: dict,
                    gain=None, adc_full: float = 65536.0) -> float:
    """高速采样 raw -> 电流 (mA)。channel 用 ch_number() 得到 0~5。"""
    key = _HS_CH.get(channel)
    if key is None:
        return float("nan")
    om_k, p_k, o_k = key
    om = cfg.get(om_k, 0.0)
    if om == 0.0:
        return float("nan")
    g = gain if gain is not None else cfg.get("g1", 1.0)
    I = (raw + cfg.get("offset", 0.0)) * cfg.get("voltage", 1.0) / adc_full / g / om * 1000.0
    I = I * (1.0 + cfg.get(p_k, 0.0)) + cfg.get(o_k, 0.0)
    return I


def voltage_from_hs(raw: int, cfg: dict, adc_mag: float = 7.8,
                    adc_full: float = 65535.0) -> float:
    """高速采样电压 raw -> 电压 (V)。

    实测: raw=10640 -> 4.20V。×7.8 缩放对应 ~25.7V 满量程(与"过压<24V"一致)。
    """
    V = (raw & 0xFFFF) * cfg.get("voltage", 1.0) / adc_full * adc_mag
    V = V * (1.0 + cfg.get("pv", 0.0)) + cfg.get("ov", 0.0)
    return V


def decode_high_speed_power(frame: dict, cfg: dict) -> dict:
    """解码一帧高速数据, 返回 {voltage_V, current_mA, power_uW, ...}。
    约定: 采样[0] = 电压, 采样[1..24] = 电流。"""
    hdr, samples, flag = parse_high_speed(frame["payload_full"])
    v = voltage_from_hs(samples[0], cfg)
    currents = []
    for i in range(1, 25):
        # 厂商代码: 电流采样序号 1..24 对应通道索引 0..23 (getChNumberToServer(array2, num3-1))
        ch = ch_number(hdr, i - 1)
        if ch <= 4:
            c = current_from_hs(samples[i], ch, cfg)
            if math.isfinite(c):          # 跳过未校准档位(om=0)导致的 NaN
                currents.append(c)
    # 取电流平均值
    I_ma = sum(currents) / len(currents) if currents else float("nan")
    P_mW = abs(v) * abs(I_ma)          # V * mA = mW
    return {
        "voltage_V": v,
        "current_mA": I_ma,
        "power_mW": P_mW,
        "power_uW": P_mW * 1000.0,
        "samples": len(currents),
        "channels": [ch_number(hdr, i - 1) for i in range(1, 25)],
    }


if __name__ == "__main__":
    # 自测: 两段式解析(含错位字节 + 配置重组)
    q = queue.Queue()
    fd = FrameDecoder(q)

    good = build_frame(CMD_RES_VERSION, b"AA-EMK850+-XX-XXXXXXXX-XXXXXXXX")
    bad = bytes([0x12, 0x99, 0x00]) + good  # 前置 3 垃圾字节 + 错位 magic
    fd.feed(bad)
    parser = ProtocolParser(q)
    parser.run()
    assert parser.version == "AA-EMK850+-XX-XXXXXXXX-XXXXXXXX", parser.version
    print("两段式解析自测通过:", parser.version, "丢弃:", fd.stats["dropped"])
