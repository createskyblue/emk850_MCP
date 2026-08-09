# EMK850+ 低功耗分析仪 · 命令行 + HTTP 控制工具

> 逆向英加 EMK850+ 串口协议，封装成 Python CLI + HTTP 接口——脚本 / CI / AI 都能直接读 µA·nW 功耗、控可编程输出。

![EMK850+ 实物图](docs/emk850_photo.jpg)

## 项目简历

**背景**：EMK850+ 是低功耗调试常用仪器，但原厂只给了一个点鼠标的上位机，毫无 API。

**痛点**：抓波形不能"只停图不停采"；采样点多就卡顿；自动降采样显示失真；AI代理无法调用官方上位机。

**做了什么**：逆向其 115200/8N1 串口协议（64 字节定长帧 + 流式状态机），封装为 Python 驱动 + FastAPI HTTP 服务。

**价值**：让脚本 / CI / AI 直接读 µA·nW 功耗、控可编程输出，补上"AI 自主低功耗优化闭环"里控制分析仪的一环。

## 它能干什么

- **读功耗**：一条命令读出电压 / 电流 / 功率，分辨率到 µA、nW（实测 `4.202 V / 6.94 µA / 29.2 µW`）。
- **控输出**：把仪器当成一个 0–3.3V+ 的可编程电压源，用来给被测板供电。
- **空载清零**：测悬空基线并扣掉，得到干净读数。
- **读版本 / 校准参数**。
- **HTTP 接口**：任何能发 HTTP 请求的客户端（脚本、CI、AI 助手）都能直接驱动仪器。

## 30 秒上手（命令行）

```bash
uv sync                                   # 或 pip install pyserial
python emk850_analyzer.py power COM19
```

```text
电压: 4.202 V   电流: 6.94 uA   功耗: 29.2 uW
```

其他命令：

```bash
python emk850_analyzer.py version COM19   # 读版本
python emk850_analyzer.py config  COM19   # 读校准参数
python emk850_analyzer.py clear   COM19   # 空载清零（⚠️ 先断开被测品！）
```

## HTTP 接口（自动化 / AI 后端）

```bash
python emk850_mcp_server.py --port COM19 --http 8000
```

启动后访问 `http://localhost:8000`。这是一个普通的 FastAPI HTTP 服务（不是 MCP 协议），任何能发 HTTP 请求的客户端都能用——脚本、CI，或 AI 助手。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/power?sample_s=0.8` | GET | 读功耗（V/I/P，自动 启动→采样→停止） |
| `/version` | GET | 设备版本 |
| `/config` | GET | 校准参数（PLConfig2） |
| `/start` · `/stop` | POST | 手动开始 / 停止采样 |
| `/output` | POST | 设输出电压：`{"state":"on","voltage":3.3}` 设 3.3V，`{"state":"off"}` 设 0mV |
| `/clear` | POST | 空载清零，需 `{"confirm":true}` |
| `/health` | GET | 服务 / 端口 / 设备状态 |
| `/port` · `/port/open` · `/port/close` | GET/POST | 串口状态与切换 |

```bash
curl -s http://localhost:8000/power
curl -s -X POST http://localhost:8000/clear -H "Content-Type: application/json" -d '{"confirm":true}'
```

## 实战：用 /output 唤醒休眠中的 MCU

低功耗调试常遇到：目标 MCU 进了 sleep/stop 模式，J-Link / ST-Link 连不上。因为仪器能当可编程电源用，让它"断电再上电"就能把芯片唤醒：

```text
1. POST /output {"state":"off"}               # 设 0mV，切断电源
2. 等 10~15 秒                                # 让芯片彻底放电
3. POST /output {"state":"on","voltage":3.3}  # 恢复 3.3V，芯片复位唤醒
4. 调试器现在能重连
```

把这套接口交给 AI 助手，你只说"调试器连不上就帮我断电重启"，它就会自己调 `/output`。

> 注：`"off"` 通过设 0mV 间接拉低输出（协议无直接关断命令）。实测设 0mV 后测量端约 2.6V，**是否真正断电请用万用表确认**——别只靠这个判断芯片已掉电。

## 协议要点（逆向所得）

串口 **115200 / 8N1**，每帧固定 **64 字节**，首字节 `0x33`：

```text
[0] 0x33   帧头
[1] cmd    命令字
[2] len    载荷长度 (0..60)
[3] seq    普通帧为 0；大数据延续帧(0x40)为帧序号
[4..63] payload（60 字节，不足补 0）
```

**坑**：第 4 字节不总是 0。原厂上位机按 64 字节整块读、尾包不足直接丢——别照抄，接收端要用逐字节流式状态机（错位只丢 1 字节就能重同步）。

完整命令表、换算公式和 PLConfig2 重组逻辑见 [`docs/EMK850_PROTOCOL.md`](docs/EMK850_PROTOCOL.md)。

## 注意事项

- **串口独占**：HTTP 服务运行时会独占端口，别同时开原厂上位机或其他程序。
- **⚠️ 清零有风险**：分析仪输入端必须悬空（不接待测品）。接有负载时清零会清掉被测品的电流基线。
- **档位限制**：设备自动换挡；未校准档（om=0）下电流无法换算，会被跳过。

## 免责声明

协议系逆向原厂上位机与特定设备所得，与厂商（英加）无关、未获认可。固件 / 型号升级可能不兼容，不保证兼容。**风险自负**。

---

🔗 项目地址：https://github.com/createskyblue/Yingjia_EMK850_low-power_analyzer_MCP
