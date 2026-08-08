# EMK850+ Low-Power Analyzer — Protocol Reverse Engineering & Python Toolkit

[English](README.md) | [中文](README.zh-CN.md)

> Reverse-engineered the serial protocol of the Yingjia (英加) EMK850+ low-power
> analyzer from its vendor host software (`EMK850+.exe`, .NET) and live probes on
> a real device, then built a Python driver and a FastAPI (MCP-style) HTTP server.

---

## Table of Contents

- [Features](#features)
- [The Reverse-Engineered Protocol](#the-reverse-engineered-protocol)
- [How It Was Reverse-Engineered](#how-it-was-reverse-engineered)
- [Quick Start (CLI)](#quick-start-cli)
- [FastAPI HTTP Server (MCP)](#fastapi-http-server-mcp)
- [Requirements](#requirements)
- [Project Structure](#project-structure)
- [Warnings](#warnings)
- [Detailed Protocol Doc](#detailed-protocol-doc)

---

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

---

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

---

## How It Was Reverse-Engineered

1. **Decompiled the vendor host** — `EMK850+.exe` is a .NET 4.7.2 WPF app.
   Decompiled it to C# with ILSpy/ilspycmd (no system .NET SDK needed — a
   portable self-contained ILSpy was downloaded). The protocol classes
   (`mpa.protocol`, `mpa.SerialManager`, `mpa/Protocol.cs`) contain the entire
   frame/command definition.
2. **Extracted the calibration math** — from `MainWindow.HandleSample` /
   `HandleSampleHighSpeed`, the raw ADC → current/voltage conversion formulas
   and the `PLConfig2` calibration struct were recovered.
3. **Probed a live device on COM19** — captured raw bytes, validated the frame
   format, discovered the `seq` byte on continuation frames (byte 3 ≠ 0), and
   confirmed the big-data config reassembly (232 bytes, sum checksum).
4. **Verified power reading** — start → stream → decode produced stable values
   (`4.202 V / 6.9 µA / 29 µW`).
5. **Verified clear** — compared baseline before/after clear
   (`7.2 µA → 0.01 µA`), confirming the PC-side zero-offset approach.

---

## Quick Start (CLI)

```bash
# Option A: uv (recommended)
uv sync                        # creates .venv from pyproject.toml
uv run python emk850_analyzer.py power COM19

# Option B: pip
pip install pyserial
python emk850_analyzer.py power COM19

# Read power (V / I / P)
# Read device version
# Clear counter (⚠️ device input MUST be floating/no-load first!)
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

---

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

---

## Requirements

```
pyserial>=3.5
fastapi>=0.100
uvicorn>=0.23
```

See [`requirements.txt`](requirements.txt).

---

## Project Structure

```
emk850_MCP/
├── emk850_analyzer.py      # Python driver (pyserial): power / clear / version / config
├── emk850_mcp_server.py    # FastAPI HTTP server (MCP-style)
├── tools/
│   └── emk850_proto.py     # Protocol library: two-stage parser + decode
├── docs/
│   └── EMK850_PROTOCOL.md  # Full reverse-engineered protocol spec (Chinese)
├── README.md               # This file (English)
└── README.zh-CN.md         # Chinese README
```

---

## Warnings

1. **Serial port is exclusive** — the HTTP server holds the port; do not run the
   vendor app / CLI at the same time.
2. **Clear is risky** — the analyzer input MUST be floating (no DUT). Clearing
   while a device is connected zeroes out its current baseline.
3. **Gear limits** — the analyzer auto-ranges. On uncalibrated gears (om=0) the
   current cannot be converted; the parser skips those samples.

---

## Detailed Protocol Doc

See [`docs/EMK850_PROTOCOL.md`](docs/EMK850_PROTOCOL.md) for the full protocol
specification: complete command table, all payload structs, calibration
constants, verified test results, and legacy notes (Chinese).
