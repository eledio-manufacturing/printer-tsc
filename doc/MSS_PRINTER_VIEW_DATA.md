# Spec: data the printer service provides to MSS for the "Tiskárny" view

Status: draft — data collection only. Implementing the view in MSS is out of scope.

Prototype of the view: <https://claude.ai/code/artifact/e5e468ca-01ef-4088-9e34-8bd3c97ae31d>

## 1. The view

A table of printers with filters (hall, status, model), a status legend and these columns:

| Column | Content |
|---|---|
| Tiskárna | printer name |
| Stav | primary status tag + optional secondary lines (severity `ok` / `warn` / `danger` / `offline` / `info`) |
| Umístění | hall + area (sortable) |
| Spotřební materiál | labels `n/total ks`, ribbon `n/total m` (TSC), or CMYK toner % (office printers) — bar colored ≥50 % ok, ≥15 % warn, else danger |
| Model | printer model (sortable) |
| Akce | print history (expandable row), Restart, Tisk (test print) |

Expanded history row shows the last jobs: time, label description, quantity, serial number.

## 2. Field ownership

| Field | Example | Source |
|---|---|---|
| `id`, `name` | `tsc-a1`, `TSC-A1` | **MSS** (printer registry). The printer id **is** the print topic (`mqtt.topic`) MSS already publishes jobs to — no new id (see §8). |
| `hall`, `area` | `Hala A`, `Osazování SMT` | **MSS** only — printer doesn't know its location. |
| `model` | `TSC TC-300` | **Service**. TSC: `~!T` or `OUT GETSETTING$("SYSTEM","INFORMATION","MODEL")`. Brother: `config.printer.model`. |
| `status[]` | `[['ok','Online'],['info','Otevřená hlava']]` | **Service** (see §4). MSS derives the primary tag. |
| offline duration | `Bez odezvy 14 min` | **Service** sends `lastSeen`; MSS computes duration. |
| `labels {n,total}` | `812/1500` | **Estimated**: MSS holds `total` (roll capacity), service reports consumption (see §5). |
| `ribbon {n,total}` | `114/300 m` | **Estimated**, same as labels. TSC only. |
| `toner {k,c,m,y}` | Konica Minolta C368 | **Not this service** — separate service (SNMP Printer-MIB `prtMarkerSuppliesLevel`), not implemented now. |
| `history[]` `{t, sn, label, qty}` | `14:32, AKM-772-410, "AKM série, 40×20 mm", 120` | **MSS** — its own printHistory rows. Each MQTT message / confirm is one label, so `qty` is MSS-side grouping of rows. **Service** adds only the failure reason (see §6). |
| Restart / Tisk actions | buttons | **Service** — requires a new inbound command channel (see §7). |

## 3. Current state of the service (baseline)

- `printer_service/printers/tsc.py` `poll_health()` polls `ESC!?` every `health_poll_interval` (default 60 s) but only **logs** changes; nothing leaves the process.
- Known TSC status bits (`STATUS_BITS`): `head_opened` 0x01, `paper_jam` 0x02, `out_of_paper` 0x04, `out_of_ribbon` 0x08, `pause` 0x10, `printing` 0x20, `other_error` 0x80, plus "unreachable" on `OSError`.
- Brother QL: status (`errors[]`, `media_type`) is queried only right before a print, over USB. Brother QL printers are USB-only — no network (`tcp://`) Brother printers will be deployed.
- Outbound data today: only `POST /api/confirmPrint?id=&status=1|2&note=`, one call per label (`printHistoryId`).
- One service instance = one printer; config has no printer identity.
- Inbound: only the print topic is subscribed.

## 4. Status snapshot

### Transport

MQTT, reusing the existing broker connection (decided).

- Topic `<topic>/status`, **retained**, QoS 1 (`<topic>` = the printer's print topic, `mqtt.topic`).
- Published on every change and at least every `health_poll_interval` as heartbeat.
- MQTT Last Will on the same topic (or `<topic>/service`) with `serviceOnline: false`, so MSS can tell "service down" apart from "printer unreachable".

### Payload

```json
{
  "printerId": "printers/tsc-a1",
  "ts": "2026-10-17T14:32:05Z",
  "serviceOnline": true,
  "serviceVersion": "1.4.0",
  "reachable": true,
  "lastSeen": "2026-10-17T14:32:05Z",
  "type": "tsc",
  "model": "TC300",
  "serial": "…",
  "firmware": "…",
  "statusCode": 1,
  "flags": ["head_opened"],
  "warnings": ["ribbon_low"],
  "since": "2026-10-17T14:30:11Z",
  "counters": {
    "labelsTotal": 182340,
    "mileageM": 5321,
    "labelsPrinted": 24688,
    "ribbonMm": 1986000
  },
  "lastMediaChange": {
    "ts": "2026-10-17T09:12:40Z",
    "cause": "head_closed",
    "counters": { "labelsPrinted": 24000, "ribbonMm": 1800000 }
  },
  "lastMedia": { "widthMm": 50, "heightMm": 25, "gapMm": 3 }
}
```

| Field | Meaning / source |
|---|---|
| `reachable` / `lastSeen` | last successful status query |
| `model`, `serial`, `firmware` | TSC `OUT GETSETTING$("SYSTEM","INFORMATION","MODEL"/"SERIAL"/"VERSION")` (`GETSETTING$` only returns data when wrapped in `OUT`), queried at startup; Brother from config |
| `statusCode` | raw `ESC!?` byte (TSC) |
| `flags` | decoded error/state bits; Brother: normalized `errors[]` strings |
| `warnings` | TSC `ESC!S` status byte #2: paper low (`A`), ribbon low (`B`) — firmware A2.08 EZD+ only |
| `since` | time the current status first appeared |
| `counters.labelsTotal` | TSC `OUT GETSETTING$("RECORD","LABEL COUNTER")` (lifetime) |
| `counters.mileageM` | TSC `~!@` / `OUT GETSETTING$("RECORD","MILAGE")` (lifetime) |
| `counters.labelsPrinted`, `counters.ribbonMm` | service-side monotonic counters, see §5 |
| `lastMediaChange` | last suspected roll change: time, cause, and service counters at that moment, see §5 |
| `lastMedia` | TSPL `SIZE`/`GAP` of the last printed job |

### Severity mapping

Flags combine (e.g. `0x0B` = ribbon + jam + head open), so the service sends all flags; MSS picks the worst as the primary tag and lists the rest as secondary lines.

| Source | Severity | Suggested text |
|---|---|---|
| `0x00` normal, `0x20` printing | ok | Online |
| `0x01` head_opened | info | Otevřená hlava |
| `0x10` pause | info | Pozastaveno |
| `ESC!S` paper low / ribbon low | warn | Docházejí štítky / páska |
| `0x04` out_of_paper, `0x08` out_of_ribbon | danger | Došlé štítky / páska (printing stopped) |
| `0x02` paper_jam, `0x80` other_error | danger | Zaseknutý papír / Chyba |
| no reply | offline | Offline + "Bez odezvy N min" from `lastSeen` |
| Brother `errors[]` ("Cover open", "No media", …) | warn / danger | mapping table per string |

## 5. Consumables

Neither TSC nor Brother QL reports remaining labels or ribbon length. TSC reports only out / low (binary). The view's `n/total` is therefore an estimate:

- **MSS** stores roll capacity per printer: labels (pcs) and ribbon (m), entered when a roll is loaded.
- **Service** keeps persistent monotonic counters (never reset):
  - `labelsPrinted` += 1 per printed label. A multi-column print counts `cols` labels per row, including padding duplicates (the batch is padded to `n_cols` and those labels are physically printed).
  - `ribbonMm` += (label height + gap) mm per printed **row**, not per label — a 6-up row uses ribbon once (TSC only).
  - Counters must survive service restart (persist to a small state file).
- **Roll change detection**: when `head_opened` → closed (`cause: "head_closed"`), `out_of_paper` clears (`"paper_restored"`) or `out_of_ribbon` clears (`"ribbon_restored"`), the service overwrites `lastMediaChange` in the status snapshot with the time, cause and current counter values, and persists it with the counters. No separate event stream — MSS does not have to listen to anything beyond the retained status.
- **MSS** remembers the `lastMediaChange.ts` it has already handled. When the snapshot shows a newer one, MSS reacts — automatically, or by asking the user in the view to confirm the roll change (and its capacity). On confirmation MSS stores `lastMediaChange.counters` as the baseline for that roll.
- MSS computes `n = total − (counters.now − baseline)`, separately for labels and ribbon.
- Only the latest detection is kept. If several happen before MSS reads the status, the latest wins — which is the one that matters for the current roll. A head open without an actual roll change only produces a detection MSS/user can ignore; baseline stays as is.
- Lifetime counters (`labelsTotal`, `mileageM`) allow cross-checking the service's counters.

## 6. Print history

MSS creates the printHistory row and publishes the job, so it already knows the id, URL, label size, printer (it picked the topic), label description and serial number. The existing confirm is sent per label right after it prints (no queueing or retry), so MSS's receive time is the print time.

The failure reason travels in the `note` param of the existing call — **MSS side done**, no new param or status codes:

```
POST /api/confirmPrint?id=266&status=2&note=paper+jam
```

**Printer side — to do:** today `note` is the raw exception text (`str(e)`), e.g. `post_print: paper_jam` or `no response to status query`. Replace it with a normalized, human-readable reason MSS can show (and group by) as is:

| `note` | Source |
|---|---|
| `head opened`, `paper jam`, `out of paper`, `out of ribbon`, `paused`, `printer error` | `tsc.STATUS_BITS` of `PrinterStatusError` (several bits → comma-joined, worst first) |
| `printer unreachable` | TSC socket `OSError` / no status reply |
| `image fetch failed` | image download / conversion failed |
| `unsupported label size WxH` | no TSC / Brother command for the dimensions |
| Brother `errors[]` strings | `BrotherStatusError`, joined |

Raw exception detail stays in the service log. `status` stays 1 / 2; `worker.TSC_FAILURE_STATUS` (planned per-reason status codes) is dropped.

## 7. Commands (Restart, Tisk)

Service subscribes to `<topic>/cmd`, replies on `<topic>/cmd/ack`.

```json
{ "requestId": "b1f…", "cmd": "restart" }
{ "requestId": "b1f…", "ok": true, "error": null, "ts": "…" }
```

| `cmd` | Behaviour |
|---|---|
| `restart` | TSC: `ESC!C` (restart without AUTO.BAS). Avoid `ESC!R` — it deletes files downloaded to printer memory. Brother: USB reset. Optional `target: "service"` restarts the service process. |
| `test_print` | prints a service-defined test label (printer id + timestamp), so it exercises the same path as real jobs. TSC `SELFTEST` would skip the service and the label size config. |
| `refresh_status` | immediate status poll + publish |

Commands must be authenticated by the broker ACL — only MSS may publish to `cmd`.

## 8. Config changes

None. The print topic (`mqtt.topic`) already identifies the printer: one service instance = one printer = one print topic, and MSS already knows which topic belongs to which printer because it publishes jobs there. `printerId` in payloads is that topic, and all new topics are derived from it: `<topic>/status`, `<topic>/cmd`, `<topic>/cmd/ack`.

Subtopics don't collide with print jobs: the service subscribes to `<topic>` exactly, which does not match `<topic>/cmd` — `cmd` gets its own subscription.

Recommended naming: print topics under a common prefix, e.g. `printers/tsc-a1`, so MSS can subscribe to all statuses with `printers/+/status`. With flat topics (e.g. `print`) MSS has to subscribe per printer.

## 9. Decisions

1. Transport: MQTT topics (no REST endpoints).
2. Toner / office printers: separate service, not implemented now.
3. Brother QL: USB only, no network Brother printers — no "unknown" status state needed.

### Open

1. Accept estimated consumables (exact remaining counts are not available from TSC / Brother QL).
