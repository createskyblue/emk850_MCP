# EMK850+ Low-Power Analyzer · Serial Driver + HTTP API

Reverse-engineered the Yingjia EMK850+ serial protocol, providing a Python CLI and a FastAPI HTTP service for automated µA/nW-level power acquisition and programmable power-supply control, closing the AI-driven low-power optimization loop.

![Yingjia EMK850+ low-power analyzer](docs/emk850_photo.jpg)

## Highlights

The stock EMK850+ analyzer has severe automation limitations: it only ships a Windows GUI with no open API, so it cannot connect to scripts, programs, or AI automation platforms, blocking automated power testing and device control and greatly limiting its use in batch testing, intelligent debugging, and low-power optimization iteration.

To address these pain points, this project reverse-engineers the protocol and upgrades the functionality, making it a perfect fit for automation scenarios:

- **Full protocol reverse**: completely reproduces the device's 64-byte fixed-frame, 0x33 header, 0x40 fragmentation serial protocol, precisely matching the device's native communication logic.

- **More robust than the vendor app**: a byte-by-byte streaming frame-sync state machine solves the byte misalignment, freezes, and data corruption caused by the vendor's block reads, greatly improving runtime robustness.

- **Lightweight and ready to use**: provides both a Python CLI and a FastAPI HTTP service, supporting script calls, CI integration, and AI automation, quickly enabling power reading and device output control.

- **Solves a core debugging pain**: supports programmable power-supply power-cycle, waking an MCU that has entered deep sleep and lost the debugger connection, resolving a core low-power debugging pain point.

- **High-precision standardized acquisition**: supports µA/nW-level precision power acquisition, with a no-load baseline-zero function to remove the device's no-load offset error, outputting clean, accurate voltage/current/power measurements; every feature is HTTP-exposed for seamless integration into any automation or AI pipeline.

## Quick start

### Read power from the CLI

```bash
uv sync
python emk850_analyzer.py power COM19
```

```text
Voltage: 4.202 V   Current: 6.94 uA   Power: 29.2 uW
```

### Run the HTTP server

```bash
python emk850_mcp_server.py --port COM19 --http 8000
```

A plain FastAPI HTTP server (not the MCP protocol). After it starts, open `http://localhost:8000`; any HTTP client works, supporting RESTful endpoints for power query, output control, zeroing, and device status.

| Endpoint | Method | Description |
|---|---|---|
| `/power?sample_s=0.8` | GET | read power (V/I/P, auto start→sample→stop) |
| `/output` | POST | set output: `{"state":"on","voltage":3.3}` → 3.3V, `{"state":"off"}` → 0mV |
| `/clear` | POST | no-load zero, requires `{"confirm":true}` |
| `/version` · `/config` · `/health` | GET | version / calibration config / service & device status |
| `/start` · `/stop` | POST | manually start / stop sampling |

## Use case: wake a sleeping MCU

In low-power scenarios the MCU enters deep sleep and the debugger disconnects; use the instrument to power-cycle it awake:

1. Cut power: `POST /output {"state":"off"}`
2. Wait 10-15 s for caps to discharge
3. Restore 3.3V: `POST /output {"state":"on","voltage":3.3}`
4. Chip resets, debugger reconnects

> Note: `"off"` sets 0mV because the protocol has no direct off command. At 0mV the sense terminals still read ~2.6V; verify with a multimeter before assuming the DUT actually lost power, don't rely on this alone to judge that the chip powered down.

## Protocol highlights (reverse-engineered)

- Serial link: **115200 / 8N1**
- Frame: fixed **64 bytes**, header `0x33`, with `0x40` continuation-frame fragmentation
- Gotcha: discard the vendor's block-read approach; use a streaming state machine to guarantee sync reliability.

Full command table and conversion math are in [`docs/EMK850_PROTOCOL.md`](docs/EMK850_PROTOCOL.md).

## Caveats

- The serial port is exclusively occupied; while the HTTP server runs, do not use the vendor app at the same time.
- No-load zeroing requires disconnecting all loads, otherwise measurement error is introduced.
- The device auto-ranges; current data on uncalibrated gears will be automatically skipped.

## Disclaimer

This project's protocol was obtained by reverse-engineering the vendor's host software and is unrelated to and not officially endorsed by the manufacturer (Yingjia / 英加). Firmware or model upgrades may cause incompatibility; use at your own risk.

---

Project: https://github.com/createskyblue/Yingjia_EMK850_low-power_analyzer_MCP
