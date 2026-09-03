# printer-tsc — Service Documentation

## For non-programmers: how this all works

Think of it as a mail courier sitting next to the printer, waiting for envelopes.

1. **Where the print command comes from.** Somewhere in ERP or MSS, a
   user clicks "print label". That system sends a message onto a queue
   (MQTT — like a Slack channel, but for programs instead of people).
   The message doesn't contain the label image itself, just an address
   to download it from, and its size in pixels.
2. **Our program (`printer-tsc`) listens on that channel.** It runs
   continuously on a small computer next to the printer (typically a
   Raspberry Pi). As soon as a message arrives, it picks it up.
3. **It downloads the label image.** Either from ERP or from MSS,
   depending on where the job came from. If the exact same label was
   requested recently (within the last 5 seconds), the image isn't
   downloaded again — the previous copy is reused, saving time and
   server load.
4. **It converts the image into printer language.** A color/grayscale
   image is turned into pure black-and-white (the printer can't do
   shades of gray) and packed into the format the printer expects.
5. **It waits briefly and groups orders together.** If several copies
   of the same label arrive back to back (e.g. 5 copies), the program
   waits about 2 seconds, collects them, and sends them to the printer
   as one batch instead of printing each separately and scrambling the
   order. For small labels on a multi-column roll (6 labels side by
   side on one strip of tape), it waits until it has collected a full
   row before printing.
6. **It sends the job to the printer and verifies it actually printed.**
   The program doesn't just trust that the data was sent — it asks the
   printer "do you have paper? are you jammed? out of ribbon?". If
   there's a problem, it doesn't keep retrying forever — it tries once,
   and if that fails, it gives up and reports the failure (see step 7).
   A physical problem (no paper, a jam) needs a person at the printer
   to fix it.
7. **It reports the outcome back to MSS.** Whether the label printed
   successfully or the print failed (out of paper, jammed, printer not
   responding), the program sends that result back to MSS so it shows
   up in the print history — including *why* it failed, not just "it
   failed".

Two printer types are supported — TSC (industrial, over network/cable)
and Brother QL (smaller, over USB or network). For each printer type
and each label size, the code has a hard-coded definition of exactly
what the print command should look like (tape size, gap between
labels, image offset) — this is tuned by hand based on real test
prints, not computed automatically.

A special **test mode** can show a preview of the label on screen
instead of actually printing — used during development so tape and
paper aren't wasted on every test.

## What this service does

`printer-tsc` is a small daemon that listens on an MQTT topic for print jobs, downloads
a label image from one of two internal systems, converts it to a printer-native bitmap,
and sends it to a physical label printer — either a TSC (TSPL-protocol, raw TCP socket)
or a Brother QL (USB or TCP, via `brother_ql_next`). After each label prints (or fails
to), it reports the outcome back to MSS via HTTP.

It runs as a single long-lived process, typically on a small machine (e.g. a Raspberry
Pi) physically near the printer.

```
MQTT broker --publish--> printer-tsc --HTTP GET--> ERP or MSS (image)
                              |
                              v
                     TSC (TCP/TSPL) or Brother QL (USB/TCP)
                              |
                              v
                     HTTP POST /api/confirmPrint --> MSS
```

## Entry point and startup (`main.py`)

On startup:

1. Loads and validates `config/config.yaml` into a Pydantic `AppConfig` (`printer_service/config.py`). If loading/validation fails, the process logs an error and exits without doing anything else.
2. Stores that config in `printer_service/runtime.py`'s module-level `config` variable — a process-wide singleton read by every other module (avoids threading config through every function call).
3. Optionally configures rotating file logging (`_configure_file_logging`), in addition to the console logging that's always on.
4. Starts the background **print worker** thread (`worker.print_worker`) — this is the thread that actually talks to the printer.
5. If using a TSC printer with `health_poll_interval` set, starts a second background thread (`tsc.poll_health`) that periodically queries printer status independent of any print job, and logs status changes (paper out, jam, head open, etc.).
6. Connects to the MQTT broker over TLS, wires up callbacks, and subscribes to the configured topic.
7. If `TEST_MODE=1` env var is set, opens a Tk preview window instead of driving a real printer (see Test mode below). Otherwise runs `client.loop_forever()` — the process's main loop is just the MQTT client loop; everything else happens in background threads and MQTT callback invocations.

Auto-reconnect to the MQTT broker is handled by `paho-mqtt`'s `reconnect_delay_set(1, 30)`.

## Configuration (`printer_service/config.py`, `config/config.yaml`)

`config.yaml` is gitignored (per-deployment secrets); `config/config.example.yaml` is the checked-in template. Validated with Pydantic at startup — any missing/malformed field fails fast with a logged error.

Top-level sections:

- **`mqtt`**: broker hostname, port (TLS, typically 8883), topic to subscribe to, and basic-auth-style username/password.
- **`erp`**: hostname + auth for the ERP system — used when image `url` is relative.
- **`mss`**: hostname + auth for MSS — used when image `url` is an absolute `https://` URL, and always used for the `POST /api/confirmPrint` callback.
- **`printer`**: a discriminated union on `type`:
  - `type: tsc` — `address`, `port` (raw TCP/TSPL), `health_poll_interval` (seconds; `None`/`0` disables the background health-poll thread).
  - `type: brother_ql` — `identifier` (a `usb://VID:PID` or `tcp://host:port` string understood by `brother_ql_next`), `model` (e.g. `QL-500`).
- **`logging`** (optional): `path` (file or directory — a directory gets `printer-tsc.log` appended), `level`, `max_bytes`/`backup_count` for log rotation. Omit for console-only logging.

## The print flow, end to end

### 1. MQTT message arrives (`printer_service/mqtt.py::message_handle`)

Expected JSON payload:

```json
{ "url": "...", "width": 106, "height": 106, "printHistoryId": 123 }
```

- `url`: either a relative path (fetched from ERP) or an absolute `https://` URL (fetched from MSS).
- `width` / `height`: the label's pixel dimensions — this is the key used everywhere downstream to decide which physical label size / TSPL command / Brother label size to use. These are **not** arbitrary; they must exactly match one of the hard-coded pixel-size tables in `printers/tsc.py` or `printers/brother.py`, or the print will fail with "no command for this size."
- `printHistoryId`: only meaningful (and only kept) when `url` starts with `https://` — it's the MSS print-history row this job should confirm/fail against. Jobs whose image comes from ERP have no MSS row to confirm, so `print_id` is `None` for those and no confirm POST is ever sent for them.

### 2. Image cache lookup (`printer_service/cache.py`)

Before fetching, `message_handle` checks an in-process cache keyed on `(url, width, height)`. This exists because the **same physical label is frequently requested more than once in a short window** (e.g. user reprints, retries, multiple copies) — the cache avoids re-downloading and re-converting the image each time.

- TTL is currently **5 seconds** (`cache.CACHE_TTL`) — a cache hit inside that window skips the HTTP fetch and the PCX/bitmap conversion entirely, going straight to the print queue.
- Cache is a plain in-memory dict guarded by a `threading.Lock` — lost on process restart, not shared across processes, not size-bounded (relies on the short TTL to keep it from growing unbounded; entries are only evicted lazily on next lookup, not proactively).
- A cache miss fetches + converts the image (`imaging.fetch_image`), stores the result, and only *then* enqueues the job. If the fetch fails, a `confirmPrint?status=2` is POSTed immediately (if there's a `print_id`) and the message is dropped — it never reaches the print queue.

### 3. Image fetch and conversion (`printer_service/imaging.py`)

`fetch_image(url)`:
- Picks ERP vs MSS auth based on whether `url` starts with `https://`.
- Downloads the raw bytes, writes to a tempfile (Pillow needs a real file/path for some format detection), opens with Pillow, converts to `L` (8-bit grayscale) mode, then deletes the tempfile.
- Converts to a 1-bit TSC bitmap via `to_bitmap_bytes`: convert to Pillow mode `'1'` (no dithering — labels are print/no-print, not photographic) and round-trip through a **PCX** file. The PCX round-trip isn't cosmetic — TSC's `BITMAP` command expects packed 1-bit-per-pixel row data, and going through PCX is the mechanism used to get Pillow's `'1'`-mode array into that exact packed byte layout via `.tobytes()`.
- Returns `(label_img: Image in 'L' mode, tsc_bitmap: bytes)` — both are cached and carried through the pipeline; Brother printing uses the grayscale image, TSC printing uses the pre-packed bitmap bytes.

Two more helpers used only for **multi-column** TSC printing (small labels ganged side-by-side on one tape run):
- `inset_margin(img, margin_px)`: shrinks the label content onto a white canvas of the same size, leaving a blank margin — absorbs a few dots of column-to-column physical misalignment so content doesn't bleed past the die-cut cell into the printed gap.
- `compose_columns(images, gap_px)`: composites several label images side by side with a gap between them, padding total width to a byte boundary. (In practice `worker._print_multi_column` currently prints each column as a separate `BITMAP` command at its own offset rather than using this composite — `compose_columns` is retained for the `TEST_MODE` preview path, which needs a single flattened image to save/show.)

### 4. The print worker and batching (`printer_service/worker.py`)

All print jobs pass through a single `queue.Queue` (`worker.print_queue`) consumed by exactly one background thread (`print_worker`). Batching exists to solve two distinct problems:

**a) Ordinary batching (`BATCH_WINDOW = 2` seconds).** After pulling a job off the queue, the worker waits up to 2 seconds accumulating more jobs, **grouped by `(url, width, height)`**. If jobs for several different labels arrive interleaved (e.g. two copies each of labels A and B, arriving A,B,A,B), they're grouped so all of A's copies print together and all of B's print together — rather than requeuing a mismatched job to the tail of the queue, which used to cause exactly the interleaved A,B,A,B print order (see git history: `963299f fix(worker): group-by-key batching instead of requeue-to-tail`). Each group is handed to `_print_batch`.

**b) Multi-column batching.** Some label sizes are configured in `tsc.MULTI_COLUMN_SIZES` as belonging to a physical multi-up tape layout (currently only `(106, 106)` pixels → a 6-column tape). For those, the worker instead waits up to `tsc.MULTI_COLUMN_WINDOW` (2s) trying to collect up to `n_cols` jobs of that same size, pads the batch to `n_cols` by repeating the last label if fewer arrived in time, and hands it to `_print_multi_column`, which builds one `BITMAP` command per physical column at its own x-offset in a single TSPL print job (not a Python-side composited image — this keeps the gap/pitch used for confirming print math decoupled from the true physical tape pitch baked into `tspl_x`/pitch).

`_print_batch` (non-multi-column path):
- **TEST_MODE**: shows the image in a Tk window, confirms all `print_id`s with status 1, returns — no real printer touched.
- **Brother QL**: calls `brother.print_labels`, one physical print job per label in the batch (not a single multi-image raster job — a prior single-stream approach jammed reliably on the trailing cut). Confirms each `print_id` to MSS **as soon as that specific label finishes**, via an `on_printed(i)` callback, so a jam partway through a batch of N leaves the already-printed labels correctly confirmed instead of all-or-nothing.
- **TSC**: same shape via `tsc.print_batch` — one `PRINT 1,1` TSPL job per label, same per-label confirm callback, same reasoning.
- On exception, confirms the *remaining unconfirmed* `print_id`s (`print_ids[confirmed:]`) with status 2 and the exception text as `note`.

`_print_multi_column` confirms all (deduplicated) `print_id`s in the batch together — it's a single physical print job, so there's no meaningful partial-success point.

### 5. TSC printer protocol details (`printer_service/printers/tsc.py`)

**Label size table.** `select_print_command(data)` is a hard-coded `if/elif` chain matching exact `(width, height)` pixel pairs to a TSPL command prefix (`DENSITY`/`SPEED`/`SIZE`/`GAP`/`CLS`/`BITMAP ...`). There is no fallback or generic path — an unrecognized `(width, height)` returns `None`, and the worker raises `ValueError("No TSC command for dimensions WxH")`. **Adding a new label size means adding a new `elif` branch here** (see `CLAUDE.md`'s "Adding a new TSC label size" section; TSPL parameter reference is `doc/TSPL_TSPL2_Programming.pdf`).

**Send throttling (`_send_throttled`, `SEND_CHUNK_SIZE=512`, `SEND_CHUNK_DELAY=0.05s`).** Sending the whole BITMAP payload in one `sendall()` was found (via repeated identical test prints) to intermittently corrupt/shift the printed image — content-independent, and only observed after moving from a switched Ethernet link to a direct Pi↔printer link. Working theory: a switch's store-and-forward behavior was incidentally pacing the data; without it, the host can burst faster than the printer's receive pipeline drains, corrupting data somewhere below the TCP layer (invisible to Ethernet error counters). Sending in small paced chunks emulates that pacing in software.

**Status verification (`send_and_verify` / `_send_and_verify_once`).** A successful TCP send does not mean the label printed — paper can run out mid-job, etc. So the flow is:
1. Open a connection, send `ESC ! ?` (`\x1b!?`), read one status byte. If it already reports an error condition (bit mask `STATUS_ERROR_MASK`), raise `PrinterStatusError` immediately — no point queuing a command against a printer that's already broken.
2. Send the actual print command (throttled).
3. Close that connection, then **poll status on fresh connections** every 0.25s until the "printing" bit (`0x20`) clears or a deadline (`2.0 + 1.5 * label_count` seconds) passes. Polls use new connections rather than the original socket because TSC firmware can close/reset the send socket at any point once printing starts — treating it as long-lived made an ordinary mid-print disconnect look like a fatal error.
4. If the final status still carries an error bit, raise `PrinterStatusError` with `phase="post_print"`. If it timed out still showing "printing", log a warning but don't fail the job (ambiguous outcome, not a confirmed failure).

Status byte bit meaning (`STATUS_BITS`): `head_opened`, `paper_jam`, `out_of_paper`, `out_of_ribbon`, `pause`, `printing`, `other_error` — combinable bitmask, e.g. `0x0B` = ribbon+jam+head. `pause`/`printing` alone are not failures.

**`PrinterStatusError`** carries the raw status byte and which phase (`pre_print`/`post_print`) it was detected in, so callers can eventually branch per failure reason. Today, `worker._confirm_status_for` always collapses to MSS status `2` (generic error) because MSS's `confirmPrint` API doesn't yet have per-reason codes — `worker.TSC_FAILURE_STATUS` is an empty dict ready to be filled in (`{"out_of_paper": 3, ...}`) once MSS grows those codes.

**Background health poll (`poll_health`)**: independent of any print job, queries status every `health_poll_interval` seconds and logs on *change* only (not every poll), distinguishing "unreachable" (socket-level failure) from a reported status code. Currently log-only — no live notification sink exists yet (flagged as a TODO in the code: candidates are an MQTT publish on the existing broker connection, or a new MSS endpoint).

**Multi-column printing (`print_multi_column`)**: builds one `BITMAP` TSPL command per column, each at x-offset `tspl_x + i * pitch` (`pitch = label_w + gap_px`), concatenated into a single `SIZE`/`GAP`/`CLS`/multiple-`BITMAP`/`PRINT 1,1` command sent as one job — so the whole strip is one physical print, verified once via `send_and_verify` with `label_count = number of columns` (affects the timeout deadline, not the pass/fail logic).

The one currently-configured multi-column size, `(106, 106)` px, is a 6-column tape (`ERT-AM009X009Z1`, 9mm sticker + 2.57mm gap) — the numeric constants in `MULTI_COLUMN_SIZES` (`gap_px=32`, `tspl_size`, `tspl_x=21`, `tspl_y=16`, `margin_px=7`) each carry an inline comment explaining a specific calibration mistake that produced that value (e.g. `tspl_y` was tuned up after QR-code top-edge clipping; `margin_px` absorbs physical column drift).

### 6. Brother QL printer details (`printer_service/printers/brother.py`)

- `BROTHER_LABEL_SIZES`: pixel-size table mapping `(width, height)` (checked in both orientations) to `brother_ql`'s label-size string + a `dpi_600` flag.
- `DPI_600_UPSCALE`: two known sizes get resized via Lanczos to a 2x pixel size before printing, to drive the printer at 600dpi instead of 300dpi for those specific label dimensions.
- USB device reset: for `usb://` identifiers, finds the device by vendor ID (`0x04f9`, Brother's USB vendor ID) and calls `dev.reset()` before printing, then sleeps 1s for the device to re-enumerate.
- `check_status`: queries printer status via `brother_ql`'s `status()` helper and raises `BrotherStatusError` only on an explicit error reported by the printer. Network (`tcp://`) identifiers don't support this status query in `brother_ql_next` (SNMP-only, unimplemented) so it's skipped for those. The query itself is known to be flaky over USB on this printer (`"Received no data"` even when printing right after works fine) — a failed *query* is only logged, never treated as a print-blocking failure; only an actual reported error byte aborts the job.
- `print_labels`: one independent raster job per label in the batch (own invalidate/initialize/cut cycle each) — a single multi-image raster stream was found to jam reliably on the trailing cut. Stops and raises as soon as one label's `send()` result reports `did_print: False`, so the worker's confirm logic only marks the labels that actually printed.

### 7. Confirming outcomes to MSS

Every successful or failed label eventually triggers `POST {mss.hostname}/api/confirmPrint?id={print_id}&status={1|2}[&note=...]`, using MSS basic auth. `status=1` = printed OK, `status=2` = failed (currently the *only* failure code in use — see the `TSC_FAILURE_STATUS` note above). `note` is the URL-encoded exception message, added so a human looking at MSS print history can see *why* a print failed (jam, timeout, HTTP fetch error, etc.) without needing to go dig in the service's log file (see git history: `5cb4d9a feat(mss): send note on confirmPrint failures`).

Jobs sourced from ERP (relative `url`, no `printHistoryId`) never get a confirm call — there's no MSS print-history row for them to update.

## Test mode

Setting `TEST_MODE=1` (or `true`) in the environment before starting the process switches every "print" into showing a Tk preview window instead of touching a real printer (or, for multi-column jobs, saving a composited PNG to disk). This lets the whole MQTT→fetch→convert→batch pipeline be exercised without hardware. Tk objects can only be touched from the main thread, so `test_preview.py` uses a thread-safe queue (`_test_ui_queue`) that worker threads push `(image, title)` onto, drained by a `root.after`-scheduled poll loop running on Tk's own main loop (started in `main.py` alongside `client.loop_start()`).

## Operational notes / known rough edges

- **Cache is per-process, in-memory, unbounded except by TTL** — restarting the service always re-fetches; very bursty distinct URLs are never evicted proactively, only lazily overwritten/ignored on next lookup after TTL expiry.
- **Label size tables are exhaustive hard-coded lists**, not computed from physical dimensions — every new label size/tape needs a code change and deploy in both `printers/tsc.py` (TSC) and `printers/brother.py` (Brother).
- **TSC gap-sensor calibration is stored on the printer itself**, independent of the `SIZE`/`GAP` sent per job — swapping tape rolls without recalibrating can silently truncate prints past the *old* tape's memorized length. `_tools/calibrate.py` is a one-off script to run after a tape swap: sends `SIZE`/`GAP`/`GAPDETECT` so the printer re-learns real physical length/gap from the sensor.
- **Failure granularity is currently flat** — both TSC and Brother failures collapse to MSS `status=2`; the richer `PrinterStatusError`/`BrotherStatusError` reasons are logged and put in the `note` field, but aren't yet mapped to distinct MSS status codes (would need MSS API support first).
- **No automated tests, no linter** configured for this repo (per `CLAUDE.md`).
