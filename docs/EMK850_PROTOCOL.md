# EMK850+ 低功耗分析仪 串口通讯协议

> 逆向来源：厂商上位机 `EMK850+.exe`（.NET 4.7.2）经 ILSpy 反编译（`tools/decompiled/`）。
> 状态：✅ 已由上位机源码确认；🟡 需在设备上实测验证；❓ 未完全确认。
> 版本：v0.1（第一阶段交付）

---

## 1. 串口参数

| 参数 | 值 | 来源 |
|---|---|---|
| 波特率 | **115200** | `MainWindow.cs:7594` |
| 数据位 | 8 | `MainWindow.cs:7595` |
| 停止位 | 1 | `MainWindow.cs:7596` |
| 校验 | None | `MainWindow.cs:7597` |
| 帧长 | 固定 **64 字节** | `SysReceiveData` 循环按 64 字节整块读取，不足 64 字节的尾包**直接丢弃** |

---

## 2. 帧结构（64 字节定长）

| 偏移 | 长度 | 字段 | 说明 |
|---|---|---|---|
| 0 | 1 | `magic` | 固定 `0x33`（51） |
| 1 | 1 | `cmd` | 命令字 |
| 2 | 1 | `len` | payload 有效长度（0~60） |
| 3 | 1 | `seq` | **实测修正**：普通帧固定 `0x00`；大块数据延续帧（0x40）是帧序号 `1,2,3...`。**不要**校验此字节恒为 0 |
| 4 | 60 | `payload` | 数据区，最多 60 字节，不足部分补 0 |

> 协议定义：`mpa/Protocol.cs`（`Protocol` 类，`Pack=1`，`PROTO_SIZE=64`）。
> 发送时始终发送完整 64 字节（`Utils.StructToBytes` 序列化整个结构体，空余补 0）。

### ⚠️ 接收策略：逐字节流式状态机

厂商上位机按 64 字节整块读取，尾包不足 64 字节直接丢弃——这是上位机的简化做法，
**Python 脚本不要照抄**。设备回传可能出现字节错位（如电源/复位瞬间的杂讯、半帧数据），
按整块切会永久失步。

推荐实现（`tools/emk850_proto.py` 已按此实现）：

```
状态机三态：
  WAIT_MAGIC   : 逐字节扫描，找到 0x33 进入 HEADER
  HEADER       : 收 cmd/len/seq 3 字节 → 只校验 len<=60
                 （seq 不校验：大块数据延续帧里是帧序号 1,2,3...，见 §2 帧表）
                 → 校验失败：丢弃当前这 1 字节，回 WAIT_MAGIC 继续
                 （注意：丢弃的是"匹配失败的那一个字节"，剩余字节继续匹配）
  PAYLOAD      : 收满整帧 64 字节 → 出帧，回 WAIT_MAGIC
                 （帧级无校验；累加和只用于 §6.3 大块数据重组）
```

关键点：
1. **每次只丢 1 个字节**，绝不整帧丢弃。
2. 任何校验失败后，不是清空缓冲，而是从"下一字节"继续扫描。
3. 收到完整帧后也要回到 WAIT_MAGIC，处理流中的下一帧。

### ⚠️ 厂商块读取缺陷证据（ilspycmd 11.0 反编译核实，2026-08-10）

厂商上位机 `EMK850+.exe`（.NET 4.7.2 WPF）实际接收逻辑（`MainWindow.cs:6576`，115200 波特率）：

```csharp
private void SysReceiveData(object sender, SerialDataReceivedEventArgs e) {
    byte[] array = new byte[64];
    while (serialPort.BytesToRead >= 64) {              // ① 只认 64 字节整块
        serialPort.Read(array, 0, 64);
        Protocol prot = (Protocol)Utils.BytesToStruct(array, typeof(Protocol)); // ② 盲解，不校验 0x33
        HandleProtocol(prot, array);                    // ③ 按垃圾 cmd 分发
        // ... 各功能窗口分发
    }
    if (serialPort.BytesToRead > 0 && serialPort.BytesToRead < 64) {
        Console.WriteLine($"2抛掉{serialPort.BytesToRead}字节数据");
        serialPort.Read(array, 0, serialPort.BytesToRead);  // ④ 尾包 <64 直接丢弃
    }
}
```

缺陷点（代码坐实）：

1. **无 0x33 头扫描、无重新同步**：唯一的对齐假设是"每块恰好 64 字节"。只要一次事件在帧中间触发（读慢 / UI 忙 / 复位瞬态杂讯），首包错位 → 尾包被丢 → 下一事件又从流中段开始 → **永久失步直到重连**。
2. **尾包无条件丢弃**：每个 `DataReceived` 事件结束时缓冲被清空（整块解析 + 尾包丢弃），`<64` 的残段直接丢掉，既丢数据又加重失步。
3. **盲解结构体**：`Utils.BytesToStruct`（`Utils.cs:318`）是 `Marshal.Copy` 直拷 64 字节，不校验 `array[0]==0x33`；错位后 `prot.cmd` 是垃圾，`HandleProtocol` 的 switch（`MainWindow.cs:3040`）静默误判/丢弃。
4. **卡死风险（校准窗口）**：`calWindow.cs:221/245` 直接 `Read(array,0,64)` 不查 `BytesToRead`，全代码未设 `ReadTimeout`（.NET 默认 `InfiniteTimeout=-1`）。复位瞬态半帧 / 短应答时，该事件线程**永久阻塞** → 校准界面卡死。

补充发现：

- `handleAllProtocol` + `serialRingBuffer`（`MainWindow.cs:6640`，10MB 环形缓冲）是**死代码**：`WriteBuff` 全工程无调用点，缓冲从未被喂数据，只有 `ReadBuff` 空转。
- `mySerialManager`（`mySerialManager.cs:228`，230400 波特率 USB VCP 管理器）的 `SysReceiveData` 是**空壳**，未接真正处理——厂商代码存在两套串口子系统且接收设计不统一。

反编译产物：`tools/decompiled/`（ilspycmd 11.0.0，137 个 .cs）。

---

## 3. 命令表

命令常量定义见 `mpa/Protocol.cs`。方向：`→` 上位机发往设备，`←` 设备发往上位机。

| cmd | 名称 | 方向 | 载荷 | 说明 |
|---|---|---|---|---|
| 16 | `CMD_REQ_VERSION` | → | 无 | 请求版本号 |
| 17 | `CMD_RES_VERSION` | ← | ASCII 字符串 | 版本响应，见 §6.1 |
| 18 | `CMD_REQ_POWERON` | → | `PLPowerOn`(6B) | 电源/电压输出开启 |
| 19 | `CMD_RES_POWERON` | ← | — | 上电应答 |
| 20 | `CMD_REQ_POWEROFF` | → | 无 | 关闭电源输出 |
| 21 | `CMD_RES_POWEROFF` | ← | — | 断电应答 |
| 22 | `CMD_REQ_START` | → | `PLStart`(4B) | **启动采样**（波形模式） |
| 24 | `CMD_REQ_STOP` | → | 无 | 停止采样（波形模式） |
| 25 | `CMD_RES_STOP` | ← | — | 停止应答 |
| 33 | `CMD_RESULT` | ← | 14×`Sample` | **采样数据帧**（实时电流/电压），见 §5 |
| 35 | `CMD_SYNC` | — | — | 同步 |
| 48 | `CMD_REQ_WRITE_CONFIG` | → | 配置数据 | 写配置 |
| 49 | `CMD_RES_WRITE_CONFIG` | ← | — | 写配置应答 |
| 50 | `CMD_REQ_READ_CONFIG` | → | 无 | 请求读取配置（触发大块数据上传） |
| 51 | `CMD_RES_READ_CONFIG` | ← | — | 读配置应答 |
| 64 | `CMD_REQ_BIG_DATA` | ← | 配置分块 | 配置大块数据的**后续分块** |
| 65 | `CMD_REW_BIG_DATA` | ← | — | 大数据应答 |
| 66 | `CMD_REQ_BIG_DATA_FIRST` | ← | 头信息+首块 | 配置大块数据**首帧**，见 §6.3 |
| 68 | `CMD_REQ_BIG_DATA_LAST` | ← | — | 大数据末帧 |
| 72 | `CMD_PROGRAM_REPONSE` | ← | — | 升级响应 |
| 73 | `CMD_PROGRAM_RESULT_REPONSE` | ← | — | 升级结果响应 |
| 75 | `CMD_PROGRAM_ENTER_PROCESS_MODE_REPONSE` | ← | — | 升级模式进入响应 |
| 77 | `CMD_PROGRAM_WAIT_RESET_RESPONSE` | ← | — | 复位等待响应 |
| 95 | `CMD_USER_STOP` | → | 无 | **停止采样**（工厂模式，连发 3 次） |
| 96 | `CMD_USER_START` | → | 无 | **启动采样**（工厂模式） |
| 97 | `CMD_USER_CONFIG_VOLT` | → | `FactoryVoltConfig`(2B) | 设置电压（工厂模式） |
| 98 | `CMD_USER_CURRENT_RESULT` | ← | 14×`SampleCurrent` | **电流数据帧**（工厂模式），见 §5.3 |
| 99 | `CMD_USER_VOLT_RESULT` | ← | 14×`Sample` | **电压数据帧**（工厂模式），见 §5.3 |
| 100 | `CMD_USER_START_CLEAR` | → | 无 | **清零开始**（空载清零） |
| 101 | `CMD_USER_END_CLEAR` | → | 无 | **清零结束** |
| 102 | `CMD_PROGRAM_SEND_ALARMPARAM` | →/← | 告警参数 | 告警参数配置 |
| 103 | `CMD_PROGRAM_SEND_ALARMPARAM2` | →/← | 告警参数2 | |
| 104 | `CMD_REQ_READ_ALARMCONFIG` | → | — | 读取告警配置 |
| 105 | `CMD_REQ_VOLTSET` | ← | 采样数据 | 电压设定回传（自动保护模式） |
| 116 | `CMD_AUTOCHECK` | → | `PLPowerOn`(6B) | 自动校验（部分型号的清除校正用） |
| 117 | `CMD_REQ_ID_PRIVATE` | →/← | 加密字符串 | 设备 ID/私有信息 |
| 119 | `CMD_REQ_START_MAX_CURRENT_CHECK` | → | — | 最大电流检测开始 |
| 120 | `CMD_REQ_STOP_MAX_CURRENT_CHECK` | → | — | 最大电流检测停止 |
| 121 | `CMD_FORCE_HALL_SENSOR` | → | 无 | 强制使用 MOS/霍尔 |
| 128 | `CMD_CLOSE_HALL_SENSOR` | → | 无 | 关闭霍尔传感器 |
| 129 | `CMD_RESULT_PROCESS` | ← | 2×short | 处理结果/错误码，见 §6.4 |
| 130 | `CMD_REQ_100K` | → | 无 | 切 100K 采样率 |
| 131 | `CMD_REQ_10K` | → | 无 | 切 10K 采样率 |
| 132 | `CMD_REQ_HIGH_SPEED_DATA` | ← | 高速采样数据 | 高速模式数据帧 |
| 133 | `CMD_STORE_100K_PARAM_PRIVATE` | → | — | 存 100K 私有参数 |
| 134 | `CMD_REQ_100K_PARAM_PRIVATE` | →/← | 加密字符串 | 100K 私有参数 |
| 181 | 校准控制命令 | →/← | 子命令+参数 | **设置输出电压(mV)等**，见 §4.8 ⭐实测有效 |
| 183 | `CMD_Lcd_Ack` | ← | — | LCD 应答 |
| 184~186 | 充电/模拟电池/恒流源数据 | ← | — | 扩展功能数据帧 |

> ⚠️ **输出电压控制的正确命令是 cmd 181 子命令 6**（§4.8），不是 cmd 18/20。
> cmd 18（REQ_POWERON）/ cmd 20（REQ_POWEROFF）在协议里有定义，但**实测对本设备输出无效**（发送后测量无变化）。

---

## 4. 载荷结构体（小端）

所有结构体均为 `Pack=1`（无对齐填充），小端序。

### 4.1 `PLPowerOn`（6 字节）— cmd 18/116
| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | ushort | `da` | DAC 值（电压对应值，约 3300~3350 对应 3V，见 `powerWenVoltSendAutoCheck`） |
| 2 | float | `da_value` | 设定的电压值 (V) |

> DAC 换算公式（源码 `SendClearCorrectCMD`）：`da = 3320 + (3.0 - volt - compensateVal) / 0.1 * 25`，型号不同基准值不同（3300/3340）。常规使用直接填 `da=4095` 也可。

### 4.2 `PLStart`（4 字节）— cmd 22
| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | short | `threshold` | 电流阈值 1 |
| 2 | short | `threshold2` | 电流阈值 2 |

### 4.3 `FactoryVoltConfig`（2 字节）— cmd 97
| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | ushort | `volt` | 电压值（= 设定 V × 10，如 3.3V → 33） |

### 4.4 `Sample`（4 字节）— cmd 33/99/105 数据单元
| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | ushort | `voltage` | 高 2 位 = 电流档位；低 12 位 = 电压 ADC |
| 2 | short | `current` | 电流原始 ADC（有符号） |

`grade = (voltage >> 14) & 0x03`：
- `0` = uA 档（om1/p1/o1）
- `1` = mA 档（om3/p3/o3）
- `2` = nA 档（om2/p2/o2）
- `3` = 特殊（JM 型号用 / 第二通道）

### 4.5 `SampleUint`（4 字节）— 5A 大电流型号
| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | ushort | `voltage` | 同 Sample |
| 2 | ushort | `current` | 电流（无符号） |

### 4.6 `SampleCurrent`（4 字节）— cmd 98 数据单元
| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | int | `current` | 电流值（工厂模式，单位 10µA → 除以 100000 得 A） |

### 4.7 `PLConfig2`（216 字节 = 27×double）— 校准配置
```csharp
double voltage, offset, max;        // 量程参考电压 / 零点偏移 / 最大量程
double g1, om1, o1, p1;             // 1 档(µA): 增益, 采样电阻(mΩ?ohm), 偏移, 斜率修正
double g2, om2, o2, p2;             // 2 档(nA)
double g3, om3, o3, p3;             // 3 档(mA)
double o4, p4, o5, p5;              // 4/5 档修正
double ov, pv;                      // 电压零点 / 电压斜率
double ch2_min, ch2_max;            // 通道2 阈值区间修正
double ch4_min, ch4_max, ch5_min, ch5_max;
```
> **实测修正**：设备实际下发的配置块为 **232 字节 = 29×double**（T2 与 5 通道型号相同）。
> 布局 = 上面 27 个字段 + `om4`（索引 [27]）+ `om5`（索引 [28]）。
> 厂商 `getParamLenByDevType()` 返回的 264/244 只是 CRC16 用的长度，**不是结构体大小**；`PLConfig2_old` 旧版为 17 doubles（136B）。
> ⚠️ channel 3/4 的电流换算系数是 `om4`/`om5`（厂商用 `m_conf2_Channel5.om4/om5`），不是 `ch4`/`ch5`。

### 4.8 `cmd 181`（0xB5）校准/输出控制命令 — ⭐实测有效

帧格式：`[0x33][0xB5][len][0x00][子命令][参数...]`，payload[0] = 子命令。

| 子命令 | len | 参数 | 功能 |
|---|---|---|---|
| **6** | 3 | `[6, mV低, mV高]` (ushort LE) | **设置输出电压 (mV)** ⭐实测有效 |
| 1 | 6 | `[1, 0, 0, 1, 1, 1]` | 清除校准参数 |
| 2 | 2 | `[2, 档位]` | 锁档（设量程档） |
| 3 | 2 | `[3, 档位]` | 锁档（校准用） |
| 4 | 1 | `[4]` | 读取电流（应答见下） |
| 7 | 1 | `[7]` | 读取电压（应答见下） |
| 9 | 9+ | `[9, 充/放, float mV, float mA, ...]` | 充放电参数 |
| 11 | 2 | `[11, 0|1]` | **断开(0)/接通(1)校准负载**（内部继电器） |

**读取电流/电压应答格式**（sub=4 / sub=7，校准内部接口，未实测）：
```
请求: [0x33][0xB5][len=1][0x00][4|7]
应答: [0x33][0xB5][len][0x00][4|7][float 值(4B, LE)...]
        ↑ payload[0]=回显子命令, payload[1..4]=float 读数
```
- sub=4 → float 电流读数；sub=7 → float 电压读数（源码 `CalibrationApp.com1_DataReceived`：`BitConverter.ToSingle(ackdata, 5)`）
- 这是**设备端校准后的单次读数**，与正常测量的连续高速流（cmd 0x84）是两套机制；
  日常读功耗用 `/power`（高速流）即可，此接口为校准时核对用。

**设置输出电压（子命令 6）实测结果**（COM19）：
```
设 3400mV → V≈3.40V ✓   设 2000mV → V≈2.65V
设 0mV → V≈2.62V（测量下限，实际输出端是否 0V 需万用表确认）
```
> 这是**真正控制分析仪电压输出**的命令（校准代码 `CalibrationApp.SendSetVoltageCommand` 使用）。
> cmd 18/20（POWERON/POWEROFF）实测对输出无效。

**⚠️ 无"直接关闭输出"的命令（实测结论）**：设备输出是**常开型电压源**——设了电压就一直输出，
协议里没有一条命令能把输出端子完全切断。只有 `sub=6 设 0mV` 能间接把输出降到最低。
实测无效的候选命令：`cmd 20`（POWEROFF）、`cmd 185 sub=3`（模拟电池停止）、
`cmd 184 sub=0`（结束测试）、`cmd 186 sub=11`（恒流源停止）、`cmd 128`（关霍尔）。
> 因此脚本/MCP 的"关输出"=`set_output_voltage(0)`（设 0mV 间接关断）。

**接/断负载（子命令 11）说明**：校准流程专用——控制内部继电器把**已知阻值的校准电阻**接通/断开到测量端：
- `[11,0]` = 断开负载 → 空载校准（测零偏基线）
- `[11,1]` = 接通负载 → 带载校准（测流过已知电阻的电流，算 K/B 增益系数）
- ⚠️ 仅出厂校准时用，正常使用**不要乱动**（会切换内部负载，影响测量回路）。

---

## 5. 采样数据解析（功耗计算核心）

### 5.1 采样数据帧（cmd 33，波形模式）

payload 布局（60 字节）：
```
[0..3]  4字节 未知头（可能是计数器）
[4..]   56字节 = 14 × Sample(4B)
```

上位机解析：`Utils.BytesToStructs(payload, typeof(Sample), 4, 14)`（`MainWindow.cs:1800`）。

### 5.2 电流 / 电压换算（单位：电流 mA，电压 mV）

源码 `HandleSample`（`MainWindow.cs:1782`）：

**电流（raw = `sample.current`）**
```
grade = (sample.voltage >> 14) & 0x03
gain  = m_conf2.g1                          // KL 型号特殊取 20 或 config.max_1
I_mA  = (raw + m_conf2.offset) * m_conf2.voltage / 65536 / gain / omX * 1000
I_mA  = I_mA * (1 + pX) + oX                // X 按档位取 1/2/3
```
其中 `omX/pX/oX` 按档位：`grade0→om1,p1,o1`，`grade1→om3,p3,o3`，`grade2→om2,p2,o2`。

**电压（raw = `sample.voltage & 0xFFF`）**
```
V_mV  = raw * m_conf2.voltage / 4096 * adc_magnification
V_mV  = V_mV * (1 + m_conf2.pv) + m_conf2.ov
adc_magnification = 7.8（默认，部分型号从告警配置读取）
```

**功耗**
```
P = V(V) × I(A)      单位 W / mW / µW ...
```

> 校准系数 `m_conf2`（PLConfig2）需通过 cmd 50 从设备读取，见 §6.3。
> 上位机还会叠加"悬空清零"基线（`zeroPiont`）和通道阈值修正（ch4/ch5），对常规测量影响极小，脚本可先忽略。

### 5.3 工厂模式数据（cmd 98 / 99）

- **cmd 98**（电流）：payload[4..] = 14×`SampleCurrent`(int)。`I_A = current / 100000.0`（源码 `HandleFactoryCurrent`，单位 A，5 位小数）。
- **cmd 99**（电压）：payload[4..] = 14×`Sample`。`V = (int)voltage / 100.0`（单位 V，源码 `HandleFactoryVolt`）。

> 工厂模式电流分辨率 10µA，适合大电流/校准场景；波形模式（cmd 33）分辨率更高，需校准系数。

---

## 6. 典型流程

### 6.1 读取版本号
```
→ 发 64B 帧 cmd=16（也可先发 cmd=24 停止，再发 cmd=16）
← 收到 cmd=17 帧，payload[0..29] = ASCII 版本字符串
```
版本字符串形如 `BT...-XXXXXXXX-...`（`-型号-版本-`），型号标记见 `MainWindow.cs` 的 `-SV-/-LV-/-CM-/-KV-` 等。设备型号由版本字符串决定后续行为。

### 6.2 启动 / 停止采样

**波形模式（workMode=0）**
```
启动：cmd=18(PLPowerOn) → 可选 → cmd=22(PLStart{threshold,threshold2})
停止：cmd=24（帧 = [0x33][0x18][len][0][...]，len 视型号为 0 或 1）
```

**工厂模式（workMode=1）**
```
启动：cmd=18(PLPowerOn) → cmd=97(FactoryVoltConfig{volt=设定V×10}) → cmd=96
停止：cmd=95 连发 3 次
```

### 6.3 读取校准配置（用于换算真实电流）

```
→ 发 cmd=50（上位机连发 3 次，间隔 1s）
← 设备返回大块数据：
   cmd=66  首帧：payload[0..3]=头信息，payload[4..7]=总长度 N（uint），
              payload[8..11]=校验和（uint，各字节累加），payload[12..]=首段数据
   cmd=64  后续分帧：payload[0..]=数据块，逐帧拼接直到 N 字节
拼接结果 = PLConfig2（216B）/ PLConfig2_Channel5（264B）/ 旧版 244B
校验：所有字节累加和 == payload[8..11]；另有 CRC16 校验（`utilsTools.CRC16`）
```

### 6.4 错误码（cmd 129）
payload[4..5] = module_id（short），payload[6..7] = error_code（short）。`0` = 无异常，`-1` = 存储芯片问题，`-2` = 存储芯片大小过小。

### 6.5 清零计数（空载清零）★核心需求
```
前提：设备悬空（不接待测产品）
→ 发 cmd=100（清零开始，CMD_USER_START_CLEAR）
→ 等待 ≥10 秒（上位机等待 10 秒后自动完成）
→ 发 cmd=101（清零结束，CMD_USER_END_CLEAR）
```
UI 文案："请确认分析仪处于空载状态（悬空-不接待测产品），10秒后自动完成清零"。
> 波形模式（workMode=0）下上位机"清零"不向设备发命令，而是在本机记录基线（`zeroPiont`）并在显示时扣除。设备端真正的清零命令是 **cmd 100/101**（工厂模式）。

---

## 7. Python 脚本建议接口

```
clear_counter()   # 发 cmd 100 → 延时 → 发 cmd 101
read_power()      # 启动采样 → 收 cmd 33 帧 → 解析 I/V → P = V×I → 停止采样
read_version()    # 发 cmd 16 → 收 cmd 17 → 返回版本字符串
```

---

## 8. 实测验证结果（COM19，设备 AA-EMK850+-XX-XXXXXXXX-XXXXXXXX）✅

| # | 验证项 | 结果 |
|---|---|---|
| 1 | 帧格式 | ✅ 64 字节定长，magic=0x33；帧头第 4 字节在普通帧=0x00、大块延续帧=帧序号 1,2,3... |
| 2 | 版本响应 | ✅ cmd 16 → cmd 17，payload[0..29]=`AA-EMK850+-XX-XXXXXXXX-XXXXXXXX`（`型号-版本-序列号`，5 段用 `-` 分隔） |
| 3 | 配置读取 | ✅ cmd 50 → cmd 0x42 首帧（payload[4..7]=总长 **232**、[8..11]=累加和 10815）+ cmd 0x40 延续帧×3，重组 232 字节 = **29 doubles**（PLConfig2 27 字段 + om4[27] + om5[28]） |
| 4 | 启动采样 | ✅ cmd 22（payload=PLStart{threshold,threshold2}）→ 设备**持续回传 cmd 0x84 高速帧**，约 400 帧/秒 |
| 5 | 高速帧结构 | ✅ 9B 通道头 + 25×`short` 采样 + 1B 标志；**采样[0]=电压**，**采样[1..24]=电流**；9B 头每 3 位=1 个采样的通道号 |
| 6 | 档位 | ✅ 设备**自动换挡**：曾见全部采样通道=2（om3=50，µA 量级）与全部通道=3（om4=0.5，mA 量级）两种状态；**channel 3 才是 mA 档** |
| 7 | 电压换算 | ✅ `V = (voltage_raw&0xFFFF) × cfg.voltage / 65535 × 7.8 × (1+pv) + ov`，**结果直接是 V**（raw≈10640 → 4.20V，×7.8 对应 ~25.7V 满量程） |
| 8 | 电流换算 | ✅ `I_mA = (raw+offset) × cfg.voltage / 65536 / gain / omX × 1000 × (1+pX) + oX`；omX 按通道取：ch0→om1、ch1→om2、ch2→om3、**ch3→om4(0.5)**、ch4→om5(0.02)；gain=g1=20 |
| 9 | 实测读数 | ✅ 修复前空载基线 V=4.202V / I≈6.9µA；修复后 channel 3 实测 **I≈6.235mA**（与厂商公式一致），不再 NaN |
| 10 | 停止采样 | ✅ cmd 24 停止后高速流结束 |
| 11 | 清零命令 | 🟡 **未实测**（当前接有被测产品，避免污染测量）。cmd 100→等待→cmd 101 由脚本实现，待悬空时验证 |

### 8.1 已验证的测量流程（读取功耗）
```
1. cmd 50          读取配置 → 重组 PLConfig2 (232B)
2. cmd 22 (PLStart) 启动采样
3. 持续收 cmd 0x84 高速帧 (~400 帧/s):
     采样[0] 电压 raw, 采样[1..24] 电流 raw (通道号由 9B 头解码)
4. 按 §5.2 公式换算 V / I, P = V × I
5. cmd 24          停止采样
```

### 8.2 遗留说明
- 配置块统一为 **232B = 29 doubles**（含 om4/om5）。脚本按首帧声明长度 + 累加和校验重组，通用。
- 设备自动换挡：通道 0/1 的 om1/om2=0 是真未校准（换算 NaN 属正常）；通道 2/3/4（om3/om4/om5）均已校准。
- 工厂模式（cmd 96/98/99）与本设备的普通模式（cmd 22/0x84）行为不同，本次未深入验证工厂模式。
- 已知缺口：channel 5 厂商用告警配置 `avg_6` 换算，脚本未覆盖（本设备用不到）。

> 交付物已落地：`emk850_analyzer.py`（pyserial 驱动）、`emk850_mcp_server.py`（FastAPI HTTP 接口）、`tools/emk850_proto.py`（两段式解析库）。
