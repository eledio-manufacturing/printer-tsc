# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync          # install dependencies
uv run main.py   # run the service
```

No tests, no linter configured.

## Architecture

Single-file service (`main.py`). Subscribes to an MQTT topic, receives print jobs, downloads the label image, and sends it to either a TSC or Brother QL printer.

### Print flow

1. MQTT message arrives: `{ url, width, height, printHistoryId? }`
2. Image fetched from ERP (relative URL) or MSS (absolute `https://` URL) with HTTP Basic Auth
3. Image converted to 1-bit PCX via Pillow, stored in a tempfile, then deleted
4. Dispatched to printer based on `config.printer.type`:
   - **TSC**: raw TCP socket on `address:port`; TSPL/TSPL2 command built by `select_print_command()` which hard-codes BITMAP params for each known `(width, height)` pixel dimension
   - **Brother QL**: USB or TCP via `brother_ql_next`; label size from config
5. On success/failure, `POST /api/confirmPrint?id=&status=` to MSS (status 1 = ok, 2 = error)

### Configuration

`config/config.yaml` (gitignored, template at `config/config.example.yaml`). Validated at startup with Pydantic `AppConfig`. Printer type uses a discriminated union on `type: tsc | brother_ql`.

MQTT uses TLS (`port: 8883`). Auto-reconnect is enabled via `reconnect_delay_set(1, 30)`.

### Adding a new TSC label size

Add an `elif` branch in `select_print_command()` matching the pixel `(width, height)` from the incoming message and supply the corresponding TSPL `SIZE`/`GAP`/`BITMAP` parameters. Reference: `doc/TSPL_TSPL2_Programming.pdf`.
