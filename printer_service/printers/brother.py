import logging

from PIL import Image
import usb.core
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from brother_ql.raster import BrotherQLRaster

logger = logging.getLogger(__name__)

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


def print_labels(label_img: Image.Image, count: int, model: str, identifier: str) -> None:
    upscale_target = DPI_600_UPSCALE.get(label_img.size)
    if upscale_target:
        logger.debug("Upscaling image %s -> %s for 600dpi", label_img.size, upscale_target)
        label_img = label_img.resize(upscale_target, Image.Resampling.LANCZOS)
    label_size, dpi_600 = select_brother_label_size(*label_img.size)
    qlr = BrotherQLRaster(model)
    instructions = convert(qlr, [label_img] * count, label_size, cut=False, dpi_600=dpi_600, compress=False, hq=True)
    if identifier.startswith('usb://'):
        dev = usb.core.find(idVendor=0x04f9)
        if dev:
            dev.reset()
    send(instructions, printer_identifier=identifier, blocking=False)
