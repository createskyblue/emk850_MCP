#!/usr/bin/env python3
"""EMK850+ 低功耗分析仪 —— FastAPI HTTP 接口(MCP 封装)。

提供 HTTP 接口调用分析仪的基本指令:
  GET  /version   读取设备版本
  GET  /power     读取实时功耗 (V/I/P), 自动 启动→采样→停止
  POST /clear     清零计数(空载清零) —— 需设备悬空空载!
  GET  /config    读取校准配置
  POST /start     手动启动采样
  POST /stop      手动停止采样
  GET  /health    服务/端口状态

用法:
  python emk850_mcp_server.py --port COM19 --http 8000

说明:
  - 服务独占串口, 运行期间请勿再用上位机或其他程序占用该端口
  - /clear 是危险操作(需空载悬空), 必须传 confirm=true 才会执行
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emk850_analyzer import Emk850Analyzer

app = FastAPI(title="EMK850+ 低功耗分析仪 MCP", version="1.0.0")

_analyzer: Emk850Analyzer | None = None
_lock = threading.Lock()


class ClearRequest(BaseModel):
    confirm: bool = False        # 必须显式确认设备空载
    wait_s: float = 10.0
    force: bool = False          # 跳过空载确认(仅测试用)


class PortRequest(BaseModel):
    port: str                    # 串口号, 如 "COM19"


def get_analyzer() -> Emk850Analyzer:
    global _analyzer
    if _analyzer is None:
        raise HTTPException(503, "分析仪未初始化")
    return _analyzer


@app.on_event("startup")
def _startup():
    pass  # 实际初始化在 main() 中完成


@app.get("/")
def index():
    a = get_analyzer()
    return {
        "service": "EMK850+ 低功耗分析仪 MCP",
        "port": a.port,
        "device": a.version or "(未读取)",
        "commands": ["/version", "/power", "/clear", "/config", "/start", "/stop",
                     "/port", "/port/open", "/port/close", "/health"],
    }


@app.get("/health")
def health():
    with _lock:
        a = get_analyzer()
        return {
            "ok": a.ser.is_open,
            "port": a.port,
            "device": a.version,
            "config_loaded": bool(a.cfg),
            "power_window_size": len(a._power_window),
        }


@app.get("/port")
def get_port():
    """获取当前串口状态。"""
    with _lock:
        return get_analyzer().get_port()


@app.post("/port/open")
def open_port(req: PortRequest):
    """打开(或切换)串口。"""
    with _lock:
        try:
            return get_analyzer().open_port(req.port)
        except Exception as e:
            raise HTTPException(400, f"打开串口 {req.port} 失败: {e}")


@app.post("/port/close")
def close_port():
    """关闭串口(后台读线程保持存活)。"""
    with _lock:
        return get_analyzer().close_port()


@app.get("/version")
def get_version():
    with _lock:
        a = get_analyzer()
        try:
            v = a.read_version()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
    return {"version": v}


@app.get("/power")
def get_power(settle_s: float = 0.3, sample_s: float = 0.8):
    with _lock:
        a = get_analyzer()
        try:
            r = a.read_power(settle_s=settle_s, sample_s=sample_s)
        except RuntimeError as e:
            raise HTTPException(503, str(e))
    if "error" in r:
        raise HTTPException(502, r["error"])
    return r


@app.get("/config")
def get_config():
    with _lock:
        a = get_analyzer()
        try:
            cfg = a.read_config()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
    if not cfg:
        raise HTTPException(502, "读取配置失败")
    return {"config": cfg}


@app.post("/start")
def start_sampling():
    with _lock:
        a = get_analyzer()
        try:
            if not a.cfg:
                a.read_config()
            a.start()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
    return {"status": "started"}


@app.post("/stop")
def stop_sampling():
    with _lock:
        a = get_analyzer()
        try:
            a.stop()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
    return {"status": "stopped"}


@app.post("/clear")
def clear_counter(req: ClearRequest):
    """清零计数(空载清零)。

    ⚠️ 必须先断开被测产品(悬空空载), 且 confirm=true。
    """
    if not req.force and not req.confirm:
        raise HTTPException(400, "拒绝执行: 清零需要设备空载(悬空), 请先断开被测产品并传 confirm=true")
    with _lock:
        a = get_analyzer()
        try:
            return a.clear_counter(wait_s=req.wait_s, require_floating=not req.force)
        except RuntimeError as e:
            raise HTTPException(503, str(e))


def main():
    global _analyzer
    ap = argparse.ArgumentParser(description="EMK850+ FastAPI MCP")
    ap.add_argument("--port", default="COM19", help="串口号 (默认 COM19)")
    ap.add_argument("--http", type=int, default=8000, help="HTTP 端口 (默认 8000)")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    args = ap.parse_args()

    _analyzer = Emk850Analyzer(args.port)
    print(f"[EMK850+] 已打开 {args.port}, 设备: {_analyzer.read_version() or '(未知)'}")
    print(f"[EMK850+] HTTP 服务: http://{args.host}:{args.http}")
    print(f"[EMK850+] 接口: /version /power /config /clear /start /stop /health")

    try:
        uvicorn.run(app, host=args.host, port=args.http, log_level="warning")
    finally:
        _analyzer.close()


if __name__ == "__main__":
    main()
