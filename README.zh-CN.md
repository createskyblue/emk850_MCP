# 把英加 EMK850+ 低功耗分析仪，改造成 AI 可驱动的自动化功耗测试台

![英加 EMK850+ 低功耗分析仪实物图](docs/emk850_photo.jpg)
<!-- 发论坛时：把上图替换为直接上传的 PixPin 实物图 PixPin_2026-08-09_22-00-52.jpg -->

> 🔗 **项目开源地址（完整代码 + 协议文档）**：https://github.com/createskyblue/Yingjia_EMK850_low-power_analyzer_MCP

> 从厂商上位机 `EMK850+.exe`（.NET）反编译 + 真机 COM19 实测，逆向出英加 EMK850+ 低功耗分析仪的串口协议，封装成 Python 驱动 + FastAPI（MCP 风格）HTTP 服务，让任何脚本、自动化工具甚至 AI 助手都能直接读功耗、控输出。

## 一、为什么做这个：厂商上位机的痛

EMK850+ 是台很实用的低功耗分析仪——能测到 µA / nW 级的电压、电流、功耗，做 IoT / MCU 休眠电流测试离不开它。但原厂配的 `EMK850+.exe` 是个点鼠标的 WPF 界面：

- 想批量跑功耗、想让 CI 自动测、想让 AI 助手替你读数据？**做不到**，它没有命令行、没有 API。
- 想远程控制、想脚本化采样？**只能手点**。

于是我把它的串口协议逆向了出来，做成了一套**能用命令行和 HTTP 调用的工具**。现在你（和你的 AI）可以直接让仪器出数。

## 二、这个项目能给你什么

- **读功耗**：启动采样，直接解出电压 / 电流 / 功耗。
- **空载清零**：测悬空基线并扣掉，得到干净读数。
- **读版本 / 校准配置**。
- **两段式流解析**：逐字节状态机拆帧，任何字节错位只丢 1 字节就能重新同步，稳。
- **FastAPI HTTP 接口**：任何 HTTP 客户端 / MCP 都能直接驱动仪器。

实测读数（COM19，EMK850+）：

```
电压: 4.202 V   电流: 6.94 µA   功耗: 29.2 µW
```

> 💡 **这东西是给 AI 用的，不是给你手敲的**
>
> 把仓库链接丢给 Claude / Codex 这类 AI，它会自己 `uv sync`、自己把服务跑起来，**直接控制这台仪器**。下面这段提示词可直接粘给你的 AI（它也会自己扫描仓库里的 README）：
>
> ```text
> 请阅读并安装这个仓库：https://github.com/createskyblue/Yingjia_EMK850_low-power_analyzer_MCP
> 按 README 说明启动 EMK850+ 分析仪的 HTTP 服务，之后我用自然语言命令你控制仪器：
> 读功耗、空载清零、设置输出，以及在调试器连不上目标 MCU 时调用 /output 断电再上电把它唤醒。
> ```
>
> 这套 HTTP / MCP 接口不是封闭的——你可以把它当一台"可编程电源 + 功耗计"，二次开发集成进你自己的上位机、自动化测试平台或 CI 流程里。
>
> 后面那条"断电重启唤醒 MCU"的指令本来就不是写给人看的，是**写给 AI 的**：你只管说"调试器连不上时帮我断电重启"，它就会调 `/output` 把芯片唤醒、让 J-Link / ST-Link 重连。

## 三、我是怎么搞定的：逆向全过程

1. **反编译厂商上位机** —— `EMK850+.exe` 是 .NET 4.7.2 WPF 程序，用 ILSpy/ilspycmd 反编译成 C#（下的是便携自包含版，不用装系统 .NET SDK）。协议类 `mpa.protocol`、`mpa.SerialManager`、`mpa/Protocol.cs` 里藏着完整的帧/命令定义。
2. **抠出校准数学** —— 从 `MainWindow.HandleSample` / `HandleSampleHighSpeed` 还原出 ADC 原始值 → 电流/电压的换算公式，以及 `PLConfig2` 校准结构体。
3. **真机 COM19 实测** —— 抓原始字节流验证帧格式，还发现了延续帧里 `seq` 字节（第 4 字节 ≠ 0）这个坑，并确认了大块配置重组（232 字节，累加和校验）。
4. **验证读功耗** —— 启动 → 收流 → 解码，得到稳定值（`4.202 V / 6.9 µA / 29 µW`）。
5. **验证清零** —— 对比清零前后基线（`7.2 µA → 0.01 µA`），确认了"空载清零是 PC 端算偏移"的本质。

## 四、一个真正好用的实战场景：让 AI 帮你唤醒休眠中的 MCU

做低功耗开发常遇到这种情况：目标 MCU 进了 sleep/stop 模式，J-Link / ST-Link 死活连不上。下面这段不是给你手动敲的——是**写给你的 AI 的**。把仓库交给它之后，你只需说一句"调试器连不上时帮我把芯片断电重启一下"，它就会调用 `/output` 接口"断电再上电"把芯片唤醒：

```
当目标 MCU 进入低功耗休眠/停止模式，调试器连不上时：
  1. POST /output  {"state":"off"}              # 设 0mV — 切断供电
  2. 等待 10~15 秒                                # 让芯片彻底掉电放电
  3. POST /output  {"state":"on","voltage":3.3}  # 恢复 3.3V — 芯片复位唤醒
  4. 此时调试器即可重新连接目标芯片
```

## 五、快速上手

```bash
# 方式 A: uv（推荐）
uv sync
uv run python emk850_analyzer.py power COM19

# 方式 B: pip
pip install pyserial
python emk850_analyzer.py power COM19
```

常用命令：

```bash
uv run python emk850_analyzer.py power COM19    # 读取功耗
uv run python emk850_analyzer.py version COM19  # 读取版本
uv run python emk850_analyzer.py config COM19   # 读取校准配置
uv run python emk850_analyzer.py clear COM19    # 清零计数（⚠️ 先悬空！）
```

```text
$ python emk850_analyzer.py power COM19
电压: 4.202 V   电流: 6.94 uA   功耗: 29.2 uW
```

### HTTP 服务（MCP 风格）

```bash
uv run python emk850_mcp_server.py --port COM19 --http 8000
# 或：pip install pyserial fastapi uvicorn
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
| `/output` | POST | 设置输出电压（cmd 181 sub=6，mV）：`{"state":"on","voltage":3.3}` 设 3.3V，`{"state":"off"}` 设 0mV（无直接关断命令，"off" 通过设 0mV 间接切断） |
| `/port` | GET | 获取当前串口状态 |
| `/port/open` | POST | 打开 / 切换串口，body `{"port":"COM19"}` |
| `/port/close` | POST | 关闭串口（后台读线程保持存活） |

```bash
curl -s http://localhost:8000/power
curl -s -X POST http://localhost:8000/clear \
  -H "Content-Type: application/json" -d '{"confirm":true}'
curl -s -X POST http://localhost:8000/port/open \
  -H "Content-Type: application/json" -d '{"port":"COM19"}'
```

## 六、协议速览（给想深入的人）

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

**关键坑**：第 4 个字节不总是 `0x00`。厂商上位机按 64 字节整块对齐读取、尾包不足直接丢弃——**不要照抄**，接收端必须用逐字节流式状态机。

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

### 换算公式

```
电流 (mA):  I = (raw + offset) × cfg.voltage / 65536 / gain / omX × 1000 × (1+pX) + oX
电压 (V):   V = (raw & 0xFFFF) × cfg.voltage / 65535 × 7.8 × (1+pv) + ov
功耗:       P = V × I
```

`cfg`（校准参数）从设备读回，是 232 字节的 double 数组（`PLConfig2`），由 0x42/0x40 大块数据帧重组而来。

### 读取功耗流程

```
1. cmd 0x32        读配置 → 重组 PLConfig2
2. cmd 0x16        启动采样
3. 持续收 cmd 0x84 高速帧 (~400 帧/秒)
4. 逐帧解码 V / I 并取平均, P = V × I
5. cmd 0x18        停止
```

### 清零计数（空载清零）

设备会回显 cmd 0x64（应答）但**不改变**高速流数据。厂商上位机的空载清零本质是 **PC 端操作**：悬空测 ~10s 基线存为偏移，后续读数扣除。本工具同样如此。

## 七、注意事项

1. **串口独占** —— HTTP 服务运行期间独占端口，勿同时运行厂商上位机 / CLI。
2. **清零有风险** —— 分析仪输入必须悬空（不接待测品）；接有负载时清零会清掉被测品的电流基线。
3. **档位限制** —— 设备自动换挡；处于未校准档位（om=0）时电流无法换算，解析器会跳过这些采样。

## 八、依赖与项目结构

依赖：

```
pyserial>=3.5
fastapi>=0.100
uvicorn>=0.23
```

见 [`requirements.txt`](requirements.txt)。

项目结构：

```
emk850_MCP/
├── emk850_analyzer.py      # Python 驱动 (pyserial): 功耗 / 清零 / 版本 / 配置
├── emk850_mcp_server.py    # FastAPI HTTP 服务 (MCP 风格)
├── tools/
│   └── emk850_proto.py     # 协议库: 两段式解析 + 解码
├── docs/
│   ├── EMK850_PROTOCOL.md  # 完整逆向协议文档（中文）
│   └── emk850_photo.jpg    # 仪器实物图
├── README.md               # 英文 README
└── README.zh-CN.md         # 本文档
```

## 九、免责声明

本项目**完全免费、非盈利**，仅供学习、个人及内部自用。与厂商（英加）**无任何关联**，未获认可或赞助。协议系逆向厂商上位机及特定设备所得，固件/型号升级可能导致不兼容，本项目不保证兼容性。**使用风险自负**，因协议不兼容、误操作（如在接有被测产品时执行清零）等造成的损失，作者概不负责。

---

🔗 **项目开源地址（代码 + 完整协议文档）**：https://github.com/createskyblue/Yingjia_EMK850_low-power_analyzer_MCP

觉得有用欢迎 Star / Fork，也欢迎在 Issues 里交流踩坑经验。
