# EMK850+ Power Analyzer Toolkit

> Reverse-engineered the Yingjia EMK850+ serial protocol into a Python CLI + HTTP API — so scripts, CI, and AI can read µA/nW power and switch its programmable output directly.

![Yingjia EMK850+ low-power analyzer](docs/emk850_photo.jpg)

## Project resume

**Background**: the EMK850+ is the go-to instrument for low-power debugging, but the vendor ships only a click-only Windows app with no API.

**Pain**: you can't pause the chart without pausing capture; the UI lags with large sample counts; auto-downsampling distorts the curve so it no longer matches the raw data.

**What I built**: reverse-engineered the 115200/8N1 serial protocol (64-byte fixed frames + streaming state machine) into a Python driver and a FastAPI HTTP server.

**Value**: lets scripts / CI / AI read µA·nW power and control the output — the "AI controls the analyzer" half of an autonomous low-power optimization loop.

## What it does

- **Read power** — one command gives voltage / current / power, down to µA and nW (measured `4.202 V / 6.94 µA / 29.2 µW`).
- **Control output** — use the instrument as a 0–3.3V+ programmable source to power the DUT.
- **No-load zero** — measure the floating baseline and subtract it for clean readings.
- **Read version / calibration config**.
- **HTTP API** — any HTTP client (scripts, CI, AI assistant) can drive the instrument.

You need the hardware. This repo just makes its serial protocol callable.

## Quick start (CLI)

```bash
uv sync                                   # or: pip install pyserial
python emk850_analyzer.py power COM19
```

```text
Voltage: 4.202 V   Current: 6.94 uA   Power: 29.2 uW
```

Other commands:

```bash
python emk850_analyzer.py version COM19   # read version
python emk850_analyzer.py config  COM19   # read calibration config
python emk850_analyzer.py clear   COM19   # no-load zero (⚠️ float the input first!)
```

## HTTP API (automation / AI backend)

```bash
python emk850_mcp_server.py --port COM19 --http 8000
```

Then hit `http://localhost:8000`. It's a plain FastAPI HTTP server (not the MCP protocol) — any client that can send an HTTP request can use it: scripts, CI, or an AI assistant.

| Endpoint | Method | Description |
|---|---|---|
| `/power?sample_s=0.8` | GET | read power (V/I/P, auto start→sample→stop) |
| `/version` | GET | device version |
| `/config` | GET | calibration config (PLConfig2) |
| `/start` · `/stop` | POST | manually start / stop sampling |
| `/output` | POST | set output: `{"state":"on","voltage":3.3}` → 3.3V, `{"state":"off"}` → 0mV |
| `/clear` | POST | no-load zero, requires `{"confirm":true}` |
| `/health` | GET | service / port / device status |
| `/port` · `/port/open` · `/port/close` | GET/POST | serial port status and switching |

```bash
curl -s http://localhost:8000/power
curl -s -X POST http://localhost:8000/clear -H "Content-Type: application/json" -d '{"confirm":true}'
```

## Use case: wake a sleeping MCU via `/output`

Low-power debugging often hits this: the target MCU is in sleep/stop mode and J-Link / ST-Link can't connect. Since the instrument works as a programmable supply, have it power-cycle the chip to wake it:

```text
1. POST /output {"state":"off"}               # set 0mV, cut power
2. wait 10-15 s                              # let the chip fully discharge
3. POST /output {"state":"on","voltage":3.3}  # restore 3.3V, chip resets & wakes
4. debugger can now reconnect
```

Hand the API to an AI assistant and just say "power-cycle it when the debugger can't connect" — it will call `/output` itself.

> Note: `"off"` sets 0mV because the protocol has no direct off command. Measured output at 0mV is ~2.6V at the sense terminals — **verify with a multimeter** before assuming the DUT actually lost power.

## Protocol highlights (reverse-engineered)

Serial link **115200 / 8N1**, fixed **64-byte** frames, header byte `0x33`:

```text
[0] 0x33   frame magic
[1] cmd    command byte
[2] len    payload length (0..60)
[3] seq    0 for normal frames; frame index for big-data continuation (0x40)
[4..63] payload (60 bytes, zero-padded)
```

**Gotcha:** byte 3 is not always `0x00`. The vendor app reads 64-byte aligned blocks and discards a short tail — don't copy that. Use a byte-by-byte streaming state machine (a single misaligned byte drops only 1 byte and resyncs).

Full command table, conversion math, and PLConfig2 reassembly are in [`docs/EMK850_PROTOCOL.md`](docs/EMK850_PROTOCOL.md).

## Caveats

- **Serial port is exclusive** — the HTTP server holds the port; don't run the vendor app or CLI at the same time.
- **⚠️ Clear is risky** — the analyzer input must be floating (no DUT). Clearing while a device is connected zeroes its current baseline.
- **Gear limits** — the analyzer auto-ranges; on uncalibrated gears (om=0) current can't be converted and those samples are skipped.

## Disclaimer

The protocol was reverse-engineered from the vendor host software and a specific device. Not affiliated with or endorsed by the manufacturer (英加 / Yingjia). Firmware/model upgrades may break compatibility; none is guaranteed. **Use at your own risk.**

---

🔗 Project: https://github.com/createskyblue/Yingjia_EMK850_low-power_analyzer_MCP
