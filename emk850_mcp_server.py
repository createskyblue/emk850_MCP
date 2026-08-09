#!/usr/bin/env python3
"""EMK850+ 低功耗分析仪 —— FastAPI 应用 + MCP 服务器。

同一套 FastAPI 路由同时提供两种接入方式:
  1. REST 接口 (原有): GET /power /version /config /health, POST /clear /output ...
  2. MCP 服务器 (fastapi-mcp): 把 REST 路由自动转换为 MCP 工具,
     通过 Streamable HTTP 传输挂载在 /mcp。

用法:
  python emk850_mcp_server.py --port COM19 --http 8000

启动后:
  - REST:   http://localhost:8000/    (浏览器直接看设备状态)
  - MCP:    http://localhost:8000/mcp (MCP 客户端 / MCP Inspector 连接此地址)

说明:
  - 服务独占串口, 运行期间请勿再用上位机或其他程序占用该端口
  - /clear 是危险操作(需空载悬空), 必须传 confirm=true 才会执行
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from fastapi_mcp import FastApiMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emk850_analyzer import Emk850Analyzer

SERVICE_NAME = "EMK850+ 低功耗分析仪"
SERVICE_DESCRIPTION = (
    "EMK850+ 低功耗分析仪 MCP 服务 (串口协议逆向 + 高速采样)。"
    "提供实时功耗读取 (V/I/P)、可编程电压输出控制 (掉电重启休眠芯片)、"
    "空载清零、串口管理等工具。"
)

app = FastAPI(
    title=f"{SERVICE_NAME} MCP",
    version="1.0.0",
    description=SERVICE_DESCRIPTION,
)

_analyzer: Emk850Analyzer | None = None
_lock = threading.Lock()


class ClearRequest(BaseModel):
    confirm: bool = False        # 必须显式确认设备空载
    wait_s: float = 10.0
    force: bool = False          # 跳过空载确认(仅测试用)


class PortRequest(BaseModel):
    port: str                    # 串口号, 如 "COM19"


class OutputRequest(BaseModel):
    voltage: float = 3.0         # 输出电压 (V), 仅 on 时有效
    state: str = "on"            # "on" | "off"


def get_analyzer() -> Emk850Analyzer:
    global _analyzer
    if _analyzer is None:
        raise HTTPException(503, "分析仪未初始化")
    return _analyzer


# 首页/状态页: 仅供浏览器查看, 不进 OpenAPI (从而不作为 MCP 工具暴露)
@app.get("/", operation_id="index", include_in_schema=False)
def index():
    a = get_analyzer()
    return {
        "service": f"{SERVICE_NAME} MCP",
        "port": a.port,
        "device": a.version or "(未读取)",
        "rest_commands": ["/version", "/power", "/clear", "/config", "/start", "/stop",
                          "/output", "/port", "/port/open", "/port/close", "/health"],
        "mcp": "POST /mcp (MCP Streamable HTTP, tools/list -> tools/call)",
    }


@app.get("/health", operation_id="health")
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


@app.get("/port", operation_id="get_port_info")
def get_port():
    """获取当前串口状态。"""
    with _lock:
        return get_analyzer().get_port()


@app.post("/port/open", operation_id="open_port")
def open_port(req: PortRequest):
    """打开(或切换)串口。"""
    with _lock:
        try:
            return get_analyzer().open_port(req.port)
        except Exception as e:
            raise HTTPException(400, f"打开串口 {req.port} 失败: {e}")


@app.post("/port/close", operation_id="close_port")
def close_port():
    """关闭串口(后台读线程保持存活)。"""
    with _lock:
        return get_analyzer().close_port()


@app.get("/version", operation_id="read_version")
def get_version():
    with _lock:
        a = get_analyzer()
        try:
            v = a.read_version()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
    return {"version": v}


@app.get("/power", operation_id="read_power")
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


@app.get("/config", operation_id="read_config")
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


@app.post("/start", operation_id="start_sampling")
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


@app.post("/stop", operation_id="stop_sampling")
def stop_sampling():
    with _lock:
        a = get_analyzer()
        try:
            a.stop()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
    return {"status": "stopped"}


@app.post("/output", operation_id="set_output")
def set_output(req: OutputRequest):
    """设置输出电压 (cmd 181 sub=6, mV)。实测有效。

    body: {"state":"on","voltage":3.3} 设 3.3V;  {"state":"off"} 设 0mV。

    ⚠️ 协议没有"直接关断输出"的命令, 输出是常开型电压源;
       "off" 通过设 0mV (cmd 181 sub=6) 间接把输出降到最低。
       实测设 0mV 后测量端读 ~2.6V (测量下限)。

    💡 典型用途 —— 低功耗芯片掉电重启 (power cycle):
       当目标芯片进入低功耗休眠/停止模式, 调试器(J-Link/ST-Link 等)无法连接时:
        1. set_output {"state":"off"}              # 设 0mV, 切断供电
        2. 等待 10~15 秒                             # 让芯片彻底掉电放电
        3. set_output {"state":"on","voltage":3.3}  # 恢复 3.3V 供电, 芯片复位唤醒
        4. 此时调试器即可重新连接目标芯片
    """
    with _lock:
        a = get_analyzer()
        try:
            if req.state == "off":
                return a.set_output_voltage(0)
            return a.set_output(voltage_V=req.voltage)
        except RuntimeError as e:
            raise HTTPException(503, str(e))


@app.post("/clear", operation_id="clear_counter")
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


# ---------------------------------------------------------------------------
# MCP 服务器: 自动把上面的 REST 路由转换为 MCP 工具, 挂载到 /mcp
# ---------------------------------------------------------------------------
# 内部用 httpx.ASGITransport 在进程内调用 FastAPI 路由:
#   - base_url 任意占位 (ASGI 传输不解析 host)
#   - 默认超时 10s 太短: clear_counter 空载采样默认 wait_s=10, 需调大
_mcp_http_client = httpx.AsyncClient(
    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
    base_url="http://apiserver",
    timeout=60.0,
)
mcp_server = FastApiMCP(
    app,
    name="emk850",
    # fastapi-mcp 0.4.0 内部 Server(name, description) 按位置传参,
    # 在 mcp 1.x 里该位置是 version 字段; 因此这里传版本号而非描述,
    # 避免 serverInfo.version 显示成一段中文描述。工具描述由路由 docstring 提供。
    description="1.0.0",
    http_client=_mcp_http_client,
)
mcp_server.mount_http(mount_path="/mcp")


def main():
    global _analyzer
    ap = argparse.ArgumentParser(description="EMK850+ FastAPI MCP")
    ap.add_argument("--port", default="COM19", help="串口号 (默认 COM19)")
    ap.add_argument("--http", type=int, default=8000, help="HTTP 端口 (默认 8000)")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    args = ap.parse_args()

    _analyzer = Emk850Analyzer(args.port)
    print(f"[EMK850+] 已打开 {args.port}, 设备: {_analyzer.read_version() or '(未知)'}")
    print(f"[EMK850+] REST 接口: http://{args.host}:{args.http}/")
    print(f"[EMK850+] MCP 接口:  http://{args.host}:{args.http}/mcp  (Streamable HTTP)")
    print(f"[EMK850+] MCP 工具:  read_power read_version read_config start_sampling "
          f"stop_sampling set_output clear_counter open_port close_port get_port_info health")

    try:
        uvicorn.run(app, host=args.host, port=args.http, log_level="warning")
    finally:
        _analyzer.close()


if __name__ == "__main__":
    main()
