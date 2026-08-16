import logging
import time

from PIL import Image
import usb.core
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send, status as query_status
from brother_ql.exceptions import BrotherQLError
from brother_ql.raster import BrotherQLRaster

logger = logging.getLogger(__name__)


class BrotherStatusError(RuntimeError):
    """Print aborted because the printer reported an error status before sending.

    Carries the raw error strings from brother_ql's status reply (e.g. "No media
    when printing", "Cover open") so callers can log the specific reason instead
    of only knowing "it failed".
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(", ".join(errors) if errors else "no response to status query")


BROTHER_LABEL_SIZES = {
    (165, 566): ("17x54", False),
    (165, 956): ("17x87", False),
    (202, 202): ("23x23", False),
    (306, 425): ("29x42", False),
    (306, 991): ("29x90", False),
    (413, 991): ("39x90", False),
    (425, 495): ("39x48", False),
    (578, 271): ("52x29", False),
    (696, 271): ("62x29", False),
    (696, 1109): ("62x100", False),
    (1392, 2218): ("62x100", True),
    (1164, 526): ("102x51", False),
    (1164, 1660): ("102x152", False),
}

DPI_600_UPSCALE = {
    (696, 1109): (1392, 2218),
    (1109, 696): (2218, 1392),
}


def select_brother_label_size(width: int, height: int) -> tuple[str, bool]:
    result = BROTHER_LABEL_SIZES.get((width, height)) or BROTHER_LABEL_SIZES.get((height, width))
    if result is None:
        raise ValueError(f"No Brother label matches pixel size {width}x{height}")
    return result


def check_status(model: str, identifier: str) -> None:
    """Query the printer's status and raise if it explicitly reports an error.

    Raw send() succeeding doesn't mean the label prints (e.g. jam, cover open,
    out of media) — this catches that before the job is sent instead of
    silently reporting success. Network identifiers don't support the status
    command in brother_ql (SNMP-only, unimplemented there), so skip for those.

    The ESC i S status query itself is flaky over this printer's USB link
    (observed "Received no data" even when a normal print immediately after
    succeeds) — a failed *query* is not evidence the printer can't print, so
    it's only logged, not treated as a reason to abort. Only an actual error
    byte reported back by the printer aborts the job.
    """
    try:
        result, _raw = query_status(printer_model=model, printer_identifier=identifier)
    except NotImplementedError:
        logger.debug("Status check unsupported for identifier %s, skipping", identifier)
        return
    except BrotherQLError as e:
        logger.warning("Status check failed, proceeding without it: %s", e)
        return
    if result['errors']:
        raise BrotherStatusError(result['errors'])


def print_labels(label_img: Image.Image, count: int, model: str, identifier: str, on_printed=None) -> None:
    upscale_target = DPI_600_UPSCALE.get(label_img.size)
    if upscale_target:
        logger.debug("Upscaling image %s -> %s for 600dpi", label_img.size, upscale_target)
        label_img = label_img.resize(upscale_target, Image.Resampling.LANCZOS)
    label_size, dpi_600 = select_brother_label_size(*label_img.size)
    if identifier.startswith('usb://'):
        dev = usb.core.find(idVendor=0x04f9)
        if dev:
            dev.reset()
            time.sleep(1)  # device re-enumerates after reset; let it settle before reopening
    check_status(model, identifier)
    # A single multi-image raster stream jams reliably on the trailing cut (reproduced
    # 2/2, with and without a blank trailing label appended -- not a software cut-command
    # distinction, see raster.py add_print()). Send each label as its own fully
    # independent job -- separate invalidate/initialize/cut cycle -- and stop as soon as
    # one doesn't confirm printing, so a jam doesn't silently eat the rest of the batch.
    for i in range(count):
        qlr = BrotherQLRaster(model)
        instructions = convert(qlr, [label_img], label_size, cut=True, dpi_600=dpi_600, compress=False, hq=True)
        result = send(instructions, printer_identifier=identifier, blocking=True)
        if not result['did_print']:
            raise RuntimeError(
                f"Label {i + 1}/{count} did not confirm printing (printer_state={result['printer_state']})"
            )
        if on_printed:
            on_printed(i)
