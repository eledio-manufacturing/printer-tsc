import logging
import queue
import time

import requests

from printer_service import imaging, runtime, test_preview
from printer_service.config import BrotherQlPrinterConfig
from printer_service.printers import brother, tsc

logger = logging.getLogger(__name__)

# 2000ms window to accumulate identical labels into a single print command
BATCH_WINDOW = 2

print_queue: queue.Queue = queue.Queue()

# MSS /api/confirmPrint only knows status 1 (ok) / 2 (error) today. When it grows
# dedicated codes per failure reason, map the tsc.STATUS_BITS name here instead of
# collapsing every failure to 2 — e.g. {"out_of_paper": 3, "paper_jam": 4, ...}.
TSC_FAILURE_STATUS: dict[str, int] = {}


def _confirm_status_for(e: Exception) -> int:
    if isinstance(e, tsc.PrinterStatusError):
        for bit, name in tsc.STATUS_BITS.items():
            if e.code & bit and name in TSC_FAILURE_STATUS:
                return TSC_FAILURE_STATUS[name]
    return 2


def _confirm_all(print_ids: list, status: int) -> None:
    cfg = runtime.config
    auth = (cfg.mss.auth.username, cfg.mss.auth.password)
    for pid in print_ids:
        if pid:
            try:
                requests.post(
                    url=f'{cfg.mss.hostname}/api/confirmPrint?id={pid}&status={status}',
                    auth=auth,
                )
            except Exception as e:
                logger.error("confirmPrint failed for id=%s: %s", pid, e)


def _print_batch(batch: list) -> None:
    job = batch[0]
    count = len(batch)
    print_ids = [j['print_id'] for j in batch]
    label_img, tsc_bitmap = job['image_data']
    cfg = runtime.config

    if count > 1:
        logger.debug("Printing batch of %d identical labels (url=%s)", count, job['url'])

    if test_preview.TEST_MODE:
        title = f"TEST batch x{count} {job['width']}x{job['height']} {job['url']}"
        test_preview.show_test_window(label_img, title)
        logger.info("TEST MODE: displayed %s", title)
        _confirm_all(print_ids, status=1)
        return

    if isinstance(cfg.printer, BrotherQlPrinterConfig):
        try:
            brother.print_labels(label_img, count, cfg.printer.model, cfg.printer.identifier)
            _confirm_all(print_ids, status=1)
        except Exception as e:
            logger.error("Error printing to Brother QL: %s", e)
            _confirm_all(print_ids, status=2)
    else:
        try:
            tsc.print_batch(cfg.printer.address, cfg.printer.port, job['msg_rx'], tsc_bitmap, count)
            _confirm_all(print_ids, status=1)
        except Exception as e:
            logger.error("Error printing to TSC: %s", e)
            _confirm_all(print_ids, status=_confirm_status_for(e))


def _print_multi_column(jobs: list, mc_cfg: dict) -> None:
    # jobs may contain the same job repeated (batch padded to n_cols) -> dedupe
    # so _confirm_all doesn't POST /api/confirmPrint more than once per id.
    print_ids = list(dict.fromkeys(j['print_id'] for j in jobs if j['print_id']))
    cfg = runtime.config

    try:
        margin_px = mc_cfg.get('margin_px', 0)

        images = [j['image_data'][0] for j in jobs]
        if margin_px:
            # Shrink content onto a white border within each label's own cell, so a
            # few dots of physical column misalignment lands on blank margin instead
            # of bleeding past the die-cut cell into the printed gap.
            images = [imaging.inset_margin(img, margin_px) for img in images]

        if test_preview.TEST_MODE:
            composite_img, _ = imaging.compose_columns(images, mc_cfg['gap_px'])
            out_path = f"test_multi_column_{int(time.time())}.png"
            composite_img.save(out_path)
            logger.info("TEST MODE: saved composite to %s", out_path)
            _confirm_all(print_ids, status=1)
            return

        bitmaps = [imaging.to_bitmap_bytes(img) for img in images] if margin_px \
            else [j['image_data'][1] for j in jobs]

        tsc.print_multi_column(cfg.printer.address, cfg.printer.port, images, bitmaps, mc_cfg)
        logger.debug("Multi-column print OK")
        _confirm_all(print_ids, status=1)
    except Exception as e:
        logger.error("Error printing multi-column to TSC: %s", e)
        _confirm_all(print_ids, status=_confirm_status_for(e))


def print_worker() -> None:
    while True:
        try:
            job = print_queue.get()
            w, h = job['width'], job['height']
            mc_cfg = tsc.MULTI_COLUMN_SIZES.get((w, h))

            if mc_cfg:
                n_cols = mc_cfg['cols']
                batch = [job]
                deadline = time.monotonic() + tsc.MULTI_COLUMN_WINDOW
                while len(batch) < n_cols:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        next_job = print_queue.get(timeout=remaining)
                        if next_job['width'] == w and next_job['height'] == h:
                            batch.append(next_job)
                        else:
                            print_queue.put(next_job)
                            break
                    except queue.Empty:
                        break
                # Pad to n_cols by repeating last label
                while len(batch) < n_cols:
                    batch.append(batch[-1])
                if len(batch) > len(set(j['url'] for j in batch)):
                    logger.debug("Multi-column: padded to %d cols (%d unique labels)", n_cols, len(set(j['url'] for j in batch)))
                _print_multi_column(batch, mc_cfg)
            else:
                batch = [job]
                deadline = time.monotonic() + BATCH_WINDOW
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        next_job = print_queue.get(timeout=remaining)
                        if (next_job['url'] == job['url']
                                and next_job['width'] == w
                                and next_job['height'] == h):
                            batch.append(next_job)
                        else:
                            print_queue.put(next_job)
                            break
                    except queue.Empty:
                        break
                _print_batch(batch)
        except Exception as e:
            logger.error("Print worker unhandled error: %s", e)
