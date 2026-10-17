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
| `id`, `name` | `tsc-a1`, `TSC-A1` | **MSS** (printer registry). Service config gets a matching `printer_id`. |
| `hall`, `area` | `Hala A`, `Osazování SMT` | **MSS** only — printer doesn't know its location. |
| `model` | `TSC TC-300` | **Service**. TSC: `~!T` or `GETSETTING$("SYSTEM","INFORMATION","MODEL")`. Brother: `config.printer.model`. |
| `status[]` | `[['ok','Online'],['info','Otevřená hlava']]` | **Service** (see §4). MSS derives the primary tag. |
| offline duration | `Bez odezvy 14 min` | **Service** sends `lastSeen`; MSS computes duration. |
| `labels {n,total}` | `812/1500` | **Estimated**: MSS holds `total` (roll capacity), service reports consumption (see §5). |
| `ribbon {n,total}` | `114/300 m` | **Estimated**, same as labels. TSC only. |
| `toner {k,c,m,y}` | Konica Minolta C368 | **Not this service.** Requires SNMP Printer-MIB (`prtMarkerSuppliesLevel`) from a separate collector. |
| `history[]` `{t, sn, label, qty}` | `14:32, AKM-772-410, "AKM série, 40×20 mm", 120` | **MSS** owns `sn` / `label` (printHistory record). **Service** adds real print time, outcome, error reason, label size (see §6). |
| Restart / Tisk actions | buttons | **Service** — requires a new inbound command channel (see §7). |

## 3. Current state of the service (baseline)

- `printer_service/printers/tsc.py` `poll_health()` polls `ESC!?` every `health_poll_interval` (default 60 s) but only **logs** changes; nothing leaves the process.
- Known TSC status bits (`STATUS_BITS`): `head_opened` 0x01, `paper_jam` 0x02, `out_of_paper` 0x04, `out_of_ribbon` 0x08, `pause` 0x10, `printing` 0x20, `other_error` 0x80, plus "unreachable" on `OSError`.
- Brother QL: status (`errors[]`, `media_type`) is queried only right before a print, and only over USB. `tcp://` identifiers have no status (brother_ql SNMP not implemented).
- Outbound data today: only `POST /api/confirmPrint?id=&status=1|2&note=`, and only for jobs carrying `printHistoryId` (absolute MSS URLs). ERP jobs (relative URLs) are never confirmed.
- One service instance = one printer; config has no printer identity.
- Inbound: only the print topic is subscribed.

## 4. Status snapshot

### Transport

Recommended: MQTT, reusing the existing broker connection.

- Topic `printers/<printer_id>/status`, **retained**, QoS 1.
- Published on every change and at least every `health_poll_interval` as heartbeat.
- MQTT Last Will on the same topic (or `printers/<printer_id>/service`) with `serviceOnline: false`, so MSS can tell "service down" apart from "printer unreachable".

Alternative: new MSS REST endpoint (`POST /api/printerStatus`) with the same body.

### Payload

```json
{
  "printerId": "tsc-a1",
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
    "labelsSinceMediaChange": 688,
    "ribbonMmSinceChange": 186000
  },
  "lastMedia": { "widthMm": 50, "heightMm": 25, "gapMm": 3 }
}
```

| Field | Meaning / source |
|---|---|
| `reachable` / `lastSeen` | last successful status query |
| `model`, `serial`, `firmware` | TSC `GETSETTING$("SYSTEM","INFORMATION","MODEL"/"SERIAL"/"VERSION")`, queried at startup; Brother from config |
| `statusCode` | raw `ESC!?` byte (TSC) |
| `flags` | decoded error/state bits; Brother: normalized `errors[]` strings |
| `warnings` | TSC `ESC!S` status byte #2: paper low (`A`), ribbon low (`B`) — firmware A2.08 EZD+ only |
| `since` | time the current status first appeared |
| `counters.labelsTotal` | TSC `GETSETTING$("RECORD","LABEL COUNTER")` (lifetime) |
| `counters.mileageM` | TSC `~!@` / `GETSETTING$("RECORD","MILAGE")` (lifetime) |
| `counters.*SinceChange` | service-side counters, see §5 |
| `lastMedia` | TSPL `SIZE`/`GAP` of the last printed job |

### Severity mapping

Flags combine (e.g. `0x0B` = ribbon + jam + head open), so the service sends all flags; MSS picks the worst as the primary tag and lists the rest as secondary lines.

| Source | Severity | Suggested text |
|---|---|---|
| `0x00` normal, `0x20` printing | ok | Online |
| `0x01` head_opened | info | Otevřená hlava |
| `0x10` pause | info | Pozastaveno |
| `ESC!S` paper low / ribbon low | warn | Docházejí štítky / páska |
| `0x04` out_of_paper, `0x08` out_of_ribbon | warn | Došlé štítky / páska (ribbon) |
| `0x02` paper_jam, `0x80` other_error | danger | Zaseknutý papír / Chyba |
| no reply | offline | Offline + "Bez odezvy N min" from `lastSeen` |
| Brother `errors[]` ("Cover open", "No media", …) | warn / danger | mapping table per string |
| Brother over `tcp://` | — | no status available; needs an "unknown" state not present in the prototype |

## 5. Consumables

Neither TSC nor Brother QL reports remaining labels or ribbon length. TSC reports only out / low (binary). The view's `n/total` is therefore an estimate:

- **MSS** stores roll capacity per printer: labels (pcs) and ribbon (m), entered when a roll is loaded.
- **Service** keeps persistent counters since last change:
  - `labelsSinceMediaChange` += 1 per printed label (multi-column print counts `cols` labels).
  - `ribbonMmSinceChange` += (label height + gap) mm per printed label (TSC only).
  - Counters must survive service restart (persist to a small state file).
- **Roll change detection**: when `head_opened` → closed, or `out_of_paper` / `out_of_ribbon` clears, service publishes an event on `printers/<printer_id>/events`:

  ```json
  { "printerId": "tsc-a1", "ts": "…", "event": "media_change_suspected", "cause": "head_closed" }
  ```

  MSS (or a user in the view) confirms and sends `reset_media_counter` (§7).
- MSS computes `n = total − usedSinceChange`.
- Lifetime counters (`labelsTotal`, `mileageM`) allow cross-checking the service's counters.

## 6. Print history

Extend `confirmPrint` with additional query/body fields (or publish `printers/<printer_id>/jobs` with the same content):

| Field | Meaning |
|---|---|
| `printerId` | which printer printed it |
| `printHistoryId` | existing `id` |
| `printedAt` | actual time the label was confirmed printed |
| `status` | existing 1 / 2 |
| `errorCode` | `tsc.STATUS_BITS` name (`paper_jam`, `out_of_paper`, …) or Brother error; see `TSC_FAILURE_STATUS` in `worker.py` |
| `note` | existing free-text error |
| `labelSize` | `{widthMm, heightMm}` |
| `batchIndex`, `batchSize` | position within a batched / multi-column print |
| `url` | label image URL |

Label description and serial number already live in MSS printHistory — the service does not send them.

**Open:** ERP jobs have no `printHistoryId`, so they never appear in history. Either MSS assigns ids to ERP jobs too, or the service reports them anonymously (`url` + size only).

## 7. Commands (Restart, Tisk, counter reset)

Service subscribes to `printers/<printer_id>/cmd`, replies on `printers/<printer_id>/cmd/ack`.

```json
{ "requestId": "b1f…", "cmd": "restart" }
{ "requestId": "b1f…", "ok": true, "error": null, "ts": "…" }
```

| `cmd` | Behaviour |
|---|---|
| `restart` | TSC: `ESC!C` (restart without AUTO.BAS). Avoid `ESC!R` — it deletes files downloaded to printer memory. Brother: USB reset only; not available over network. Optional `target: "service"` restarts the service process. |
| `test_print` | prints a fixed built-in test label |
| `reset_media_counter` | `{ "what": "labels" \| "ribbon" }` — after a roll change |
| `refresh_status` | immediate status poll + publish |

Commands must be authenticated by the broker ACL — only MSS may publish to `cmd`.

## 8. Config changes

```yaml
printer_id: tsc-a1          # must match the printer id in MSS
status:
  topic_prefix: printers    # printers/<printer_id>/{status,events,jobs,cmd}
```

## 9. Open decisions

1. Transport: MQTT topics (recommended) vs new MSS REST endpoints.
2. Accept estimated consumables (exact remaining counts are not available from TSC / Brother QL).
3. ERP jobs in history (§6).
4. Toner / office printers — separate collector, not this service.
5. Status for network Brother printers — add an "unknown" state to the view, or require USB.
