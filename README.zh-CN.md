# EMK850+ 低功耗分析仪 —— 协议逆向与 Python 工具集

[English](README.md) | [中文](README.zh-CN.md)

> 从厂商上位机 `EMK850+.exe`（.NET）反编译 + 真机 COM19 实测，逆向出英加 EMK850+
> 低功耗分析仪的串口通讯协议，并封装为 Python 驱动与 FastAPI（MCP 风格）HTTP 服务。

---

## 免责声明

本项目**完全免费、非盈利**，仅供学习、个人及内部自用。

- 项目**仅用于作者自动化测试自用，供 AI 工具调用**；与厂商（英加）**无任何关联**，
  未获厂商认可或赞助。
- 本协议是通过逆向厂商上位机及特定设备得到的。**后续固件/型号升级可能导致协议变更
  或不兼容**，本项目**不保证**与未来版本的兼容性。
- **使用风险自负**。因协议不兼容、误操作（如在接有被测产品时执行清零）或任何其他
  使用方式造成的损失，**作者概不负责**。

---

## 目录

- [免责声明](#免责声明)
- [功能](#功能)
- [逆向出的协议结构](#逆向出的协议结构)
- [逆向过程](#逆向过程)
- [快速上手（命令行）](#快速上手命令行)
- [FastAPI HTTP 服务（MCP）](#fastapi-http-服务mcp)
- [依赖](#依赖)
- [项目结构](#项目结构)
- [注意事项](#注意事项)
- [完整协议文档](#完整协议文档)

---

## 功能

- **读取功耗** —— 启动采样，解码电压 / 电流 / 功耗。
- **清零计数（空载清零）** —— 测量悬空基线并扣除。
- **读取设备版本与校准配置**。
- **两段式流解析** —— 逐字节状态机（拆帧）→ 帧队列 → 按命令分发；任何字节错位
  只丢 1 字节并重新同步。
- **FastAPI HTTP 接口** —— 任何 HTTP 客户端 / MCP 均可调用分析仪。

实测读数（COM19，EMK850+ 低功耗分析仪）：

```
电压: 4.202 V   电流: 6.94 µA   功耗: 29.2 µW
```

---

## 逆向出的协议结构

### 串口参数

| 参数 | 值 |
|---|---|
| 波特率 | **115200** |
| 数据位 / 停止位 / 校验 | **8N1** |
| 帧长 | **固定 64 字节** |

### 帧格式

```
[0] 0x33 (magic)
[1] cmd        命令字
[2] len        载荷长度 (0..60)
[3] seq        序号/标志 —— 普通帧固定 0x00；
               大块数据延续帧(0x40)为帧序号 1,2,3...
[4..63] payload (60 字节, 不足补 0)
```

**关键坑：** 第 4 个字节不总是 `0x00`。厂商上位机按 64 字节整块对齐读取、
尾包不足直接丢弃——**不要照抄**，接收端必须用逐字节流式状态机。

### 关键命令

| cmd | 名称 | 方向 | 载荷 |
|---|---|---|---|
| 0x10/0x11 | REQ/RES_VERSION | →/← | ASCII 版本字符串 |
| 0x16 | REQ_START | → | `PLStart{short threshold; short threshold2}` |
| 0x18 | REQ_STOP | → | — |
| 0x21 | RESULT (采样) | ← | 14×`Sample{ushort voltage; short current}` |
| 0x32 | REQ_READ_CONFIG | → | — |
| 0x42 | BIG_DATA_FIRST | ← | 头12B+数据; `[4..7]=总长`, `[8..11]=累加和` |
| 0x40 | BIG_DATA (延续) | ← | 数据块 |
| 0x64/0x65 | USER_START/END_CLEAR | → | — |
| 0x84 | HIGH_SPEED_DATA | ← | 9B 通道头 + 25×`short` + 1B 标志 |

### 采样数据（cmd 0x84 高速帧）

```
payload = [9字节通道头][25×short 采样][1字节标志]
          sample[0]       = 电压 ADC
          samples[1..24]  = 电流 ADC
每个采样的通道号 = 9字节头中的 3 位 (0..4)
```

### 换算公式

```
电流 (mA):  I = (raw + offset) × cfg.voltage / 65536 / gain / omX × 1000 × (1+pX) + oX
电压 (V):   V = (raw & 0xFFFF) × cfg.voltage / 65535 × 7.8 × (1+pv) + ov
功耗:       P = V × I
```

`cfg`（校准参数）从设备读回，是 232 字节的 double 数组（`PLConfig2`），
由 0x42/0x40 大块数据帧重组而来。

### 读取功耗流程

```
1. cmd 0x32        读配置 → 重组 PLConfig2
2. cmd 0x16        启动采样
3. 持续收 cmd 0x84 高速帧 (~400 帧/秒)
4. 逐帧解码 V / I 并取平均, P = V × I
5. cmd 0x18        停止
```

### 清零计数（空载清零）

设备会回显 cmd 0x64（应答）但**不改变**高速流数据。厂商上位机普通模式的
空载清零本质是 **PC 端操作**：悬空测 ~10s 基线存为偏移，后续读数扣除。
本工具同样如此。

---

## 逆向过程

1. **反编译厂商上位机** —— `EMK850+.exe` 是 .NET 4.7.2 WPF 程序。用 ILSpy/ilspycmd
   反编译为 C#（无需系统装 .NET SDK，下载便携自包含版）。协议类
   （`mpa.protocol`、`mpa.SerialManager`、`mpa/Protocol.cs`）含完整帧/命令定义。
2. **提取校准数学** —— 从 `MainWindow.HandleSample` / `HandleSampleHighSpeed`
   恢复 ADC 原始值 → 电流/电压的换算公式与 `PLConfig2` 校准结构体。
3. **真机 COM19 实测** —— 抓原始字节流验证帧格式，发现延续帧的 `seq` 字节
   （第 4 字节 ≠ 0），确认大块配置重组（232 字节，累加和校验）。
4. **验证读取功耗** —— 启动 → 流 → 解码得到稳定值（`4.202 V / 6.9 µA / 29 µW`）。
5. **验证清零** —— 对比清零前后基线（`7.2 µA → 0.01 µA`），确认 PC 端零点偏移方案。

---

## 快速上手（命令行）

```bash
# 方式 A: uv (推荐)
uv sync                        # 从 pyproject.toml 创建 .venv
uv run python emk850_analyzer.py power COM19

# 方式 B: pip
pip install pyserial
python emk850_analyzer.py power COM19
```

常用命令:
```bash
uv run python emk850_analyzer.py power COM19    # 读取功耗
uv run python emk850_analyzer.py version COM19  # 读取版本
uv run python emk850_analyzer.py config COM19   # 读取校准配置
uv run python emk850_analyzer.py clear COM19    # 清零计数 (⚠️ 先悬空!)
```

```text
$ python emk850_analyzer.py power COM19
电压: 4.202 V   电流: 6.94 uA   功耗: 29.2 uW
```

---

## FastAPI HTTP 服务（MCP）

```bash
# uv
uv sync
uv run python emk850_mcp_server.py --port COM19 --http 8000

# 或 pip
pip install pyserial fastapi uvicorn
python emk850_mcp_server.py --port COM19 --http 8000
```

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 服务 / 端口 / 设备状态 |
| `/version` | GET | 设备版本 |
| `/power?sample_s=0.8` | GET | 读取功耗（V/I/P，自动 启动→采样→停止） |
| `/config` | GET | 校准配置（PLConfig2） |
| `/start` | POST | 手动启动采样 |
| `/stop` | POST | 手动停止采样 |
| `/clear` | POST | 清零计数 —— 需 `{"confirm":true}` 且输入悬空 |
| `/output` | POST | 设置输出电压（cmd 181 sub=6，mV）：`{"state":"on","voltage":3.3}` 设 3.3V，`{"state":"off"}` 设 0mV。实测有效 |
| `/port` | GET | 获取当前串口状态 |
| `/port/open` | POST | 打开 / 切换串口，body `{"port":"COM19"}` |
| `/port/close` | POST | 关闭串口（后台读线程保持存活） |

```bash
curl -s http://localhost:8000/power
curl -s -X POST http://localhost:8000/clear \
  -H "Content-Type: application/json" -d '{"confirm":true}'
curl -s http://localhost:8000/port
curl -s -X POST http://localhost:8000/port/open \
  -H "Content-Type: application/json" -d '{"port":"COM19"}'
curl -s -X POST http://localhost:8000/port/close
```

---

## 依赖

```
pyserial>=3.5
fastapi>=0.100
uvicorn>=0.23
```

见 [`requirements.txt`](requirements.txt)。

---

## 项目结构

```
emk850_MCP/
├── emk850_analyzer.py      # Python 驱动 (pyserial): 功耗 / 清零 / 版本 / 配置
├── emk850_mcp_server.py    # FastAPI HTTP 服务 (MCP 风格)
├── tools/
│   └── emk850_proto.py     # 协议库: 两段式解析 + 解码
├── docs/
│   └── EMK850_PROTOCOL.md  # 完整逆向协议文档（中文）
├── README.md               # 英文 README
└── README.zh-CN.md         # 本文档
```

---

## 注意事项

1. **串口独占** —— HTTP 服务运行期间独占端口，勿同时运行厂商上位机 / CLI。
2. **清零有风险** —— 分析仪输入必须悬空（不接待测品）；接有负载时清零会清掉
   被测品的电流基线。
3. **档位限制** —— 设备会自动换挡；处于未校准档位（om=0）时电流无法换算，
   解析器会跳过这些采样。

---

## 完整协议文档

完整协议规范见 [`docs/EMK850_PROTOCOL.md`](docs/EMK850_PROTOCOL.md)：
全部命令表、载荷结构体、校准常量、实测验证结果与遗留说明（中文）。
