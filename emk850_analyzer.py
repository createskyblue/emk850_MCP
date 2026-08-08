#!/usr/bin/env python3
"""EMK850+ 低功耗分析仪 Python 驱动。

功能:
  - read_power()   读取实时功耗 (V / I / P), 自动 启动采样→解码→停止
  - clear_counter() 清零计数(空载清零): cmd 100 → 等待 → cmd 101
  - read_version() 读取版本号
  - read_config()  读取校准配置 (PLConfig2)

架构: 两段式解析
  第一段 FrameDecoder : 逐字节流式状态机, 字节流 -> 64字节数据帧, 入队列
  第二段 ProtocolParser: 从队列消费帧, 按 cmd 分发 (版本/配置/采样)
  详情见 tools/emk850_proto.py 与 EMK850+_PROTOCOL.md

用法:
  命令行:
    python emk850_analyzer.py power COM19
    python emk850_analyzer.py version COM19
    python emk850_analyzer.py config COM19
    python emk850_analyzer.py clear COM19        # 需设备悬空空载!
  库:
    from emk850_analyzer import Emk850Analyzer
    a = Emk850Analyzer("COM19")
    print(a.read_power())
    a.close()
"""
from __future__ import annotations

import os
import sys
import time
import threading
import queue
import struct
import statistics
import math

import serial

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
from emk850_proto import (
    FrameDecoder, ProtocolParser,
    build_frame,
    decode_high_speed_power,
    CMD_REQ_READ_CONFIG, CMD_REQ_START, CMD_REQ_STOP, CMD_REQ_VERSION,
    CMD_RESULT, CMD_REQ_HIGH_SPEED_DATA,
    CMD_USER_START_CLEAR, CMD_USER_END_CLEAR,
)


class Emk850Analyzer:
    """EMK850+ 低功耗分析仪驱动。

    打开串口后启动后台读线程: 字节流 -> FrameDecoder -> 队列 -> ProtocolParser。
    调用高层方法即可, 无需关心底层帧。
    """

    def __init__(self, port: str = "COM19", baudrate: int = 115200,
                 timeout: float = 0.2):
        self.baudrate = baudrate
        self.timeout = timeout
        self.port = port
        self.ser = serial.Serial(port, baudrate, timeout=timeout)
        self.ser.reset_input_buffer()
        self._io_lock = threading.Lock()

        self._q = queue.Queue()
        self.decoder = FrameDecoder(self._q)
        self.parser = ProtocolParser(self._q)

        self.cfg: dict = {}          # 校准配置 PLConfig2
        self.version: str | None = None
        self.zero_offset_mA: float = 0.0   # 空载清零基线(PC端), 读功耗时扣除
        self._config_events = queue.Queue()   # 配置完成信号
        self._power_window: list[dict] = []   # 滚动功率样本窗口

        self.parser.on_config = self._on_config
        self.parser.on_version = lambda v: setattr(self, "version", v)
        self.parser.on_any = self._on_any

        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ---------------- 底层 ----------------
    def _read_loop(self):
        while not self._stop.is_set():
            try:
                with self._io_lock:
                    if not self.ser.is_open:
                        time.sleep(0.02)
                        continue
                    n = self.ser.in_waiting
                    if n:
                        self.decoder.feed(self.ser.read(n))
                        continue
                time.sleep(0.005)
            except Exception:
                time.sleep(0.01)

    def _require_open(self):
        """端口未打开时抛 RuntimeError。"""
        if not self.ser.is_open:
            raise RuntimeError(f"串口 {self.port} 未打开")

    def _drain(self, timeout: float = 0.05):
        """消费队列里的帧(分发到处理器)。"""
        self.parser.run(timeout=timeout)

    # ---------------- 串口管理 ----------------
    def get_port(self) -> dict:
        """获取当前串口信息。"""
        with self._io_lock:
            return {
                "port": self.port,
                "is_open": self.ser.is_open,
                "baudrate": self.baudrate,
                "config_loaded": bool(self.cfg),
                "device": self.version,
            }

    def open_port(self, port: str) -> dict:
        """打开(或切换)串口。关闭旧端口并打开新端口。"""
        with self._io_lock:
            if self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.port = port
            self.ser = serial.Serial(port, self.baudrate, timeout=self.timeout)
            self.ser.reset_input_buffer()
        self.cfg = {}
        self.version = None
        return self.get_port()

    def close_port(self) -> dict:
        """关闭串口(后台读线程保持存活, 可随时 reopen)。"""
        with self._io_lock:
            if self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
        return self.get_port()

    def _on_config(self, cfg: dict):
        self.cfg = cfg
        try:
            self._config_events.put_nowait(True)
        except queue.Full:
            pass

    def _on_any(self, frame: dict):
        if frame["cmd"] == CMD_REQ_HIGH_SPEED_DATA and self.cfg:
            try:
                self._power_window.append(decode_high_speed_power(frame, self.cfg))
            except Exception:
                pass

    def _send(self, cmd: int, payload: bytes = b""):
        self._require_open()
        with self._io_lock:
            self.ser.write(build_frame(cmd, payload))
        time.sleep(0.03)

    # ---------------- 高层命令 ----------------
    def read_version(self, retries: int = 2) -> str:
        """读取版本号。返回形如 'AA-EMK850+-XX-XXXXXXXX-XXXXXXXX'。"""
        self.version = None
        for _ in range(retries):
            self._send(CMD_REQ_VERSION)
            t0 = time.time()
            while time.time() - t0 < 1.0 and self.version is None:
                self._drain(0.05)
            if self.version:
                return self.version
        return ""

    def read_config(self, retries: int = 3) -> dict:
        """读取校准配置 (PLConfig2)。先停采样再读, 避免数据流干扰。"""
        self._send(CMD_REQ_STOP)
        time.sleep(0.1)
        self.cfg = {}
        for _ in range(retries):
            self._send(CMD_REQ_READ_CONFIG)
            t0 = time.time()
            while time.time() - t0 < 1.5:
                self._drain(0.05)
                if self.cfg:
                    return self.cfg
        return {}

    def start(self):
        """启动采样 (cmd 22, PLStart{threshold=0, threshold2=0x7FFF})。"""
        pl = struct.pack("<hh", 0, 0x7FFF)
        self._send(CMD_REQ_START, pl)

    def stop(self):
        """停止采样 (cmd 24)。"""
        self._send(CMD_REQ_STOP)
        time.sleep(0.05)

    def read_power(self, settle_s: float = 0.3, sample_s: float = 0.8) -> dict:
        """读取实时功耗。

        流程: 停止 → 读配置(如缺) → 启动采样 → 收高速帧 → 解码平均 → 停止
        返回: {voltage_V, current_uA, current_ma, power_uW, samples, ...}
        """
        self.stop()
        if not self.cfg:
            self.read_config()
        if not self.cfg:
            return {"error": "读取配置失败"}

        self._power_window.clear()
        self.start()
        time.sleep(settle_s)
        t0 = time.time()
        while time.time() - t0 < sample_s:
            self._drain(0.05)
        self.stop()

        win = list(self._power_window)
        if not win:
            return {"error": "未收到采样数据"}
        vs = [r["voltage_V"] for r in win if math.isfinite(r["voltage_V"])]
        cs = [r["current_mA"] for r in win if math.isfinite(r["current_mA"])]
        ps = [r["power_uW"] for r in win if math.isfinite(r["power_uW"])]
        if not cs:
            return {"error": "采样有效电流数据为空(可能处于未校准档位)"}

        def j(v):
            """NaN/inf -> None (JSON 安全)。"""
            return None if not math.isfinite(v) else v

        # 扣除空载清零偏移
        cur_mA = statistics.mean(cs) - self.zero_offset_mA
        cur_uA = cur_mA * 1000.0
        min_uA = (min(cs) - self.zero_offset_mA) * 1000.0
        max_uA = (max(cs) - self.zero_offset_mA) * 1000.0
        return {
            "voltage_V": j(statistics.mean(vs)),
            "voltage_min_V": j(min(vs)),
            "voltage_max_V": j(max(vs)),
            "current_uA": j(cur_uA),
            "current_min_uA": j(min_uA),
            "current_max_uA": j(max_uA),
            "current_mA": j(cur_mA),
            "power_uW": j(abs(statistics.mean(vs)) * abs(cur_mA) * 1000.0),
            "power_mW": j(abs(statistics.mean(vs)) * abs(cur_mA)),
            "zero_offset_uA": j(self.zero_offset_mA * 1000.0),
            "samples": len(win),
            "port": self.port,
            "device": self.version,
        }

    def clear_counter(self, wait_s: float = 10.0, require_floating: bool = True) -> dict:
        """清零计数 (空载清零)。

        ⚠️ 前提: 分析仪输入端必须悬空空载(不接待测产品)!
        实测结论: cmd 100/101 设备会回显确认(0x64), 但不影响高速流式数据;
        厂商上位机普通模式的清零本质是【PC 端】：悬空测 wait_s 秒基线,
        存为 zero_offset, 后续读功耗时扣除。

        流程: 测空载基线 → 存偏移 → 另发 cmd100/101 与设备同步(无副作用)。
        """
        if require_floating:
            print("⚠️  清零前请确认分析仪输入端处于【空载状态(悬空-不接待测产品)】")
        self.stop()
        if not self.cfg:
            self.read_config()

        # 测空载基线(采样 wait_s 秒, 取电流均值)
        self._power_window.clear()
        self.start()
        print(f"清零中... 空载采样 {wait_s}s")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            self._drain(0.05)
        self.stop()

        cs = [r["current_mA"] for r in self._power_window
              if math.isfinite(r["current_mA"])]
        if not cs:
            return {"cleared": False, "error": "未收到采样数据"}
        baseline = statistics.mean(cs)
        self.zero_offset_mA = baseline

        # 与设备同步(实测设备回显 0x64 确认, 对高速流无影响)
        self._send(CMD_USER_START_CLEAR)
        time.sleep(0.1)
        self._send(CMD_USER_END_CLEAR)

        return {
            "cleared": True,
            "wait_s": wait_s,
            "zero_offset_uA": baseline * 1000.0,
            "zero_offset_mA": baseline,
            "port": self.port,
        }

    def close(self):
        self._stop.set()
        try:
            self.stop()
            self.ser.close()
        except Exception:
            pass


def _cli():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd, port = sys.argv[1], sys.argv[2]
    a = Emk850Analyzer(port)
    try:
        if cmd == "power":
            r = a.read_power()
            if "error" in r:
                print(r)
            else:
                print(f"电压: {r['voltage_V']:.3f} V   "
                      f"电流: {r['current_uA']:.3f} uA   "
                      f"功耗: {r['power_uW']:.2f} uW")
        elif cmd == "version":
            print("版本:", a.read_version())
        elif cmd == "config":
            cfg = a.read_config()
            for k, v in cfg.items():
                print(f"  {k:10s} = {v}")
        elif cmd == "clear":
            a.clear_counter()
        else:
            print(__doc__)
            return 1
    finally:
        a.close()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
