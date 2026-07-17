import os
import tempfile

import requests
from PIL import Image

from printer_service import runtime


def fetch_image(url: str) -> tuple[Image.Image, bytes]:
    """Download and convert image. Returns (L-mode PIL Image, 1-bit bitmap bytes for TSC)."""
    cfg = runtime.config
    if url.startswith("https://"):
        auth = (cfg.mss.auth.username, cfg.mss.auth.password)
        r = requests.get(url, auth=auth)
    else:
        auth = (cfg.erp.auth.username, cfg.erp.auth.password)
        r = requests.get(cfg.erp.hostname + url, auth=auth)

    fd, path = tempfile.mkstemp()
    pcx_path = path + ".pcx"
    try:
        os.write(fd, r.content)
        os.close(fd)
        label_img = Image.open(path).convert('L')
        label_img.convert('1', dither=Image.Dither.NONE).save(pcx_path)
        label = Image.open(pcx_path)
        label.load()
        tsc_bitmap = label.tobytes()
    finally:
        for f in (path, pcx_path):
            if os.path.exists(f):
                os.remove(f)

    return label_img, tsc_bitmap


def compose_columns(images: list[Image.Image], gap_px: int) -> tuple[Image.Image, bytes]:
    """Composite images side-by-side with gap_px white pixels between each. Returns (L-mode Image, 1-bit TSC bitmap bytes)."""
    n = len(images)
    h = images[0].height
    total_w = sum(img.width for img in images) + (n - 1) * gap_px
    padded_w = (total_w + 7) // 8 * 8  # avoid black padding bits at row end
    canvas = Image.new('L', (padded_w, h), color=255)
    x = 0
    for img in images:
        canvas.paste(img, (x, 0))
        x += img.width + gap_px

    fd, pcx_path = tempfile.mkstemp(suffix='.pcx')
    os.close(fd)
    try:
        canvas.convert('1', dither=Image.Dither.NONE).save(pcx_path)
        label = Image.open(pcx_path)
        label.load()
        tsc_bitmap = label.tobytes()
    finally:
        if os.path.exists(pcx_path):
            os.remove(pcx_path)

    return canvas, tsc_bitmap
