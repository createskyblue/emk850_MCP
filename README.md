# EMK850+ Low-Power Analyzer — Reverse-Engineered Protocol + Python/MCP Toolkit

![Yingjia EMK850+ low-power analyzer](docs/emk850_photo.jpg)

> 🔗 **Project & full protocol docs**: https://github.com/createskyblue/Yingjia_EMK850_low-power_analyzer_MCP
>
> [English](README.md) | [中文](README.zh-CN.md)

> Reverse-engineered the serial protocol of the Yingjia (英加) EMK850+ low-power
> analyzer from its vendor host software (`EMK850+.exe`, .NET) and live probes on
> a real device, then built a Python driver and a FastAPI (MCP-style) HTTP server.

## Why this exists

The EMK850+ is a handy low-power analyzer — it reads voltage / current / power
down to the µA / nW range, which is exactly what you need for IoT / MCU sleep-current
testing. But the vendor's `EMK850+.exe` is a click-through WPF GUI:

- Want to batch-test power, wire it into CI, or let an AI assistant read the data?
  **Impossible** — there is no CLI and no API.
- Want to drive it remotely or script the sampling? You can only click by hand.

So I reverse-engineered its serial protocol and turned it into a tool you (and your
AI) can actually drive from the command line and over HTTP.

## Features

- **Read power consumption** — start sampling, decode voltage / current / power.
- **Clear counter (no-load zero)** — measure the floating baseline and subtract it.
- **Read device version & calibration config**.
- **Two-stage stream parser** — byte-level state machine (frame extraction) →
  frame queue → command dispatch. Any misaligned byte drops only 1 byte and resyncs.
- **FastAPI HTTP interface** — control the analyzer from any HTTP client / MCP.

Real measured readings (COM19, EMK850+ low-power analyzer):

```
Voltage: 4.202 V   Current: 6.94 µA   Power: 29.2 µW
```

> 💡 **This is built for AI, not for you to type by hand**
>
> Hand the repo link to an AI like Claude or Codex and it will run `uv sync` and start
> the server itself — **driving the instrument directly**. The prompt below can be pasted
> straight into your AI (it will also scan the repo's README on its own):
>
> ```text
> Read and set up this repo: https://github.com/createskyblue/Yingjia_EMK850_low-power_analyzer_MCP
> Start the EMK850+ analyzer's HTTP server per the README, then let me control the
> instrument in natural language: read power, no-load zero, set output, and power-cycle
> the target via /output when the debugger can't connect to the MCU.
> ```
>
> This HTTP / MCP interface isn't a black box — treat it as a "programmable power supply
> + power meter" and integrate it into your own host app, automated test bench, or CI pipeline.
>
> The "power-cycle to wake the MCU" routine below isn't written for a human to type; it's
> **for the AI**. Just say "power-cycle it when the debugger can't connect" and it will call
> `/output` to wake the chip and let J-Link / ST-Link reconnect.

## How it was reverse-engineered

1. **Decompiled the vendor host** — `EMK850+.exe` is a .NET 4.7.2 WPF app.
   Decompiled it to C# with ILSpy/ilspycmd (portable self-contained build, no
   system .NET SDK needed). The protocol classes (`mpa.protocol`,
   `mpa.SerialManager`, `mpa/Protocol.cs`) contain the whole frame/command spec.
2. **Extracted the calibration math** — from `MainWindow.HandleSample` /
   `HandleSampleHighSpeed`, recovered the raw ADC → current/voltage conversion
   formulas and the `PLConfig2` calibration struct.
3. **Probed a live device on COM19** — captured raw bytes, validated the frame
   format, discovered the `seq` byte on continuation frames (byte 3 ≠ 0), and
   confirmed the big-data config reassembly (232 bytes, sum checksum).
4. **Verified power reading** — start → stream → decode produced stable values
   (`4.202 V / 6.9 µA / 29 µW`).
5. **Verified clear** — compared baseline before/after clear
   (`7.2 µA → 0.01 µA`), confirming the PC-side zero-offset approach.

## Practical use case: let your AI wake a sleeping MCU

Low-power debugging problem: a target MCU is in sleep/stop mode and the debugger
(J-Link / ST-Link, ...) cannot connect. The routine below isn't for you to type by
hand — it's **for your AI**. Once you've handed it the repo, just say "help me
power-cycle the target when the debugger can't connect," and it will call the
`/output` endpoint to wake the chip:

```
When a target MCU is in low-power sleep/stop mode and the debugger cannot connect:
  1. POST /output  {"state":"off"}              # 0 mV — power down
  2. wait 10–15 s                                # let the chip fully discharge
  3. POST /output  {"state":"on","voltage":3.3}  # restore 3.3 V — chip resets & wakes
  4. debugger can now connect to the target
```

## Quick Start (CLI)

```bash
# Option A: uv (recommended)
uv sync                        # creates .venv from pyproject.toml
uv run python emk850_analyzer.py power COM19

# Option B: pip
pip install pyserial
python emk850_analyzer.py power COM19
```

Commands:
```bash
uv run python emk850_analyzer.py power COM19    # read power
uv run python emk850_analyzer.py version COM19  # read version
uv run python emk850_analyzer.py config COM19   # read calibration config
uv run python emk850_analyzer.py clear COM19    # clear counter (⚠️ floating first!)
```

```text
$ python emk850_analyzer.py power COM19
Voltage: 4.202 V   Current: 6.94 uA   Power: 29.2 uW
```

## FastAPI HTTP Server (MCP)

```bash
# uv
uv sync
uv run python emk850_mcp_server.py --port COM19 --http 8000

# or pip
pip install pyserial fastapi uvicorn
python emk850_mcp_server.py --port COM19 --http 8000
```

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | service / port / device status |
| `/version` | GET | device version |
| `/power?sample_s=0.8` | GET | read power (V/I/P; auto start→sample→stop) |
| `/config` | GET | calibration config (PLConfig2) |
| `/start` | POST | start sampling manually |
| `/stop` | POST | stop sampling manually |
| `/clear` | POST | clear counter — requires `{"confirm":true}` and floating input |
| `/output` | POST | set output voltage (cmd 181 sub=6, mV): `{"state":"on","voltage":3.3}` → 3.3 V, `{"state":"off"}` → 0 mV. No direct output-off command exists, so "off" cuts the output indirectly via 0 mV |
| `/port` | GET | get current serial port status |
| `/port/open` | POST | open / switch serial port, body `{"port":"COM19"}` |
| `/port/close` | POST | close serial port (reader thread stays alive) |

```bash
curl -s http://localhost:8000/power
curl -s -X POST http://localhost:8000/clear \
  -H "Content-Type: application/json" -d '{"confirm":true}'
curl -s http://localhost:8000/port
curl -s -X POST http://localhost:8000/port/open \
  -H "Content-Type: application/json" -d '{"port":"COM19"}'
curl -s -X POST http://localhost:8000/port/close
```

## The Reverse-Engineered Protocol

### Serial link

| Param | Value |
|---|---|
| Baud rate | **115200** |
| Data bits / Stop / Parity | **8N1** |
| Frame length | **Fixed 64 bytes** |

### Frame format

```
[0] 0x33 (magic)
[1] cmd        command byte
[2] len        payload length (0..60)
[3] seq        sequence/flag — 0x00 for normal frames;
               for big-data continuation frames (cmd 0x40) this is the frame index 1,2,3...
[4..63] payload  (60 bytes, zero-padded)
```

**Important gotcha:** byte 3 is *not* always `0x00`. The vendor host reads
64-byte aligned blocks and *discards* any trailing partial block — do **not**
copy that behavior. Use a byte-by-byte streaming state machine instead.

### Key commands

| cmd | name | direction | payload |
|---|---|---|---|
| 0x10/0x11 | REQ/RES_VERSION | →/← | ASCII version string |
| 0x16 | REQ_START | → | `PLStart{short threshold; short threshold2}` |
| 0x18 | REQ_STOP | → | — |
| 0x21 | RESULT (sample) | ← | 14×`Sample{ushort voltage; short current}` |
| 0x32 | REQ_READ_CONFIG | → | — |
| 0x42 | BIG_DATA_FIRST | ← | hdr(12B) + data; `[4..7]=total len`, `[8..11]=checksum` |
| 0x40 | BIG_DATA (cont.) | ← | data chunk |
| 0x64/0x65 | USER_START/END_CLEAR | → | — |
| 0x84 | HIGH_SPEED_DATA | ← | 9B ch-hdr + 25×`short` + 1B flag |

### Sample data (cmd 0x84, high-speed frames)

```
payload = [9-byte channel header][25 × short samples][1-byte flag]
          sample[0]    = voltage ADC
          samples[1..24] = current ADC
channel no. per sample = 3 bits in the 9-byte header (0..4)
```

### Conversion math

```
Current (mA):  I = (raw + offset) × cfg.voltage / 65536 / gain / omX × 1000 × (1+pX) + oX
Voltage (V):   V = (raw & 0xFFFF) × cfg.voltage / 65535 × 7.8 × (1+pv) + ov
Power:         P = V × I
```

`cfg` (calibration) is read from the device as a 232-byte block of doubles
(`PLConfig2`), assembled from the 0x42/0x40 big-data frames.

### Read-power flow

```
1. cmd 0x32           read config → reassemble PLConfig2
2. cmd 0x16           start sampling
3. continuously receive cmd 0x84 high-speed frames (~400 fps)
4. decode V / I per frame, average, P = V × I
5. cmd 0x18           stop
```

### Clear counter (no-load zero)

The device echoes cmd 0x64 (ack) but **does not** change its high-speed stream.
In the vendor app the no-load zero is a **PC-side** operation: measure the
floating baseline for ~10 s, store it as an offset, subtract from later readings.
This tool does the same.

## Requirements

```
pyserial>=3.5
fastapi>=0.100
uvicorn>=0.23
```

See [`requirements.txt`](requirements.txt).

## Project Structure

```
emk850_MCP/
├── emk850_analyzer.py      # Python driver (pyserial): power / clear / version / config
├── emk850_mcp_server.py    # FastAPI HTTP server (MCP-style)
├── tools/
│   └── emk850_proto.py     # Protocol library: two-stage parser + decode
├── docs/
│   ├── EMK850_PROTOCOL.md  # Full reverse-engineered protocol spec (Chinese)
│   └── emk850_photo.jpg    # Instrument photo
├── README.md               # This file (English)
└── README.zh-CN.md         # Chinese README
```

## Warnings

1. **Serial port is exclusive** — the HTTP server holds the port; do not run the
   vendor app / CLI at the same time.
2. **Clear is risky** — the analyzer input MUST be floating (no DUT). Clearing
   while a device is connected zeroes out its current baseline.
3. **Gear limits** — the analyzer auto-ranges. On uncalibrated gears (om=0) the
   current cannot be converted; the parser skips those samples.

## Disclaimer

This project is **free and non-profit** — released for learning, personal, and
internal use only. It is **not** affiliated with, endorsed by, or sponsored by the
manufacturer (英加 / Yingjia). The protocol was reverse-engineered from the vendor
host software and a specific device; firmware/model upgrades may change or break
it, and **no compatibility is guaranteed**. **Use at your own risk** — the author
accepts **no responsibility** for any damage or loss caused by protocol
incompatibility, misuse, or any other use of this software.

---

🔗 **Project & full protocol docs**: https://github.com/createskyblue/Yingjia_EMK850_low-power_analyzer_MCP
