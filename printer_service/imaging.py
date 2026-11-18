import os
import tempfile
from urllib.parse import urlsplit

import requests
from PIL import Image

from printer_service import runtime

# (connect, read) seconds. Without a timeout a hung ERP/MSS blocks the MQTT network
# loop forever (fetch runs inside the message callback): no keepalive, broker drops
# the connection and QoS 0 jobs published meanwhile are lost.
HTTP_TIMEOUT = (5, 30)


def _same_origin(url: str, base: str) -> bool:
    # Only called for https:// URLs; treat an explicit :443 like no port, so
    # "https://mss.example.com:443/x" still matches "https://mss.example.com".
    a, b = urlsplit(url), urlsplit(base)
    return (a.scheme, a.hostname, a.port or 443) == (b.scheme, b.hostname, b.port or 443)


def fetch_image(url: str) -> tuple[Image.Image, bytes]:
    """Download and convert image. Returns (L-mode PIL Image, 1-bit bitmap bytes for TSC)."""
    cfg = runtime.config
    if url.startswith("https://"):
        # Only send MSS credentials to the MSS host itself -- the URL comes from the
        # MQTT message, so anyone able to publish to the topic could otherwise point
        # it at their own server and collect the credentials.
        auth = (cfg.mss.auth.username, cfg.mss.auth.password) if _same_origin(url, cfg.mss.hostname) else None
        r = requests.get(url, auth=auth, timeout=HTTP_TIMEOUT)
    else:
        auth = (cfg.erp.auth.username, cfg.erp.auth.password)
        r = requests.get(cfg.erp.hostname + url, auth=auth, timeout=HTTP_TIMEOUT)
    # Otherwise an HTML error page (401/404/500) reaches Pillow and surfaces only as
    # an opaque "cannot identify image file".
    r.raise_for_status()

    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, r.content)
        os.close(fd)
        label_img = Image.open(path).convert('L')
    finally:
        if os.path.exists(path):
            os.remove(path)

    return label_img, to_bitmap_bytes(label_img)


def to_bitmap_bytes(img: Image.Image) -> bytes:
    """Convert an L-mode image to 1-bit TSC bitmap bytes via a PCX round-trip."""
    fd, pcx_path = tempfile.mkstemp(suffix='.pcx')
    os.close(fd)
    try:
        img.convert('1', dither=Image.Dither.NONE).save(pcx_path)
        label = Image.open(pcx_path)
        label.load()
        return label.tobytes()
    finally:
        if os.path.exists(pcx_path):
            os.remove(pcx_path)


def inset_margin(img: Image.Image, margin_px: int) -> Image.Image:
    """Shrink content onto a white canvas of the original size, leaving margin_px blank
    on each edge. Used in multi-column TSC printing so a few dots of physical
    misalignment between tape columns land on blank margin instead of spilling
    past the die-cut cell into the printed gap."""
    if margin_px <= 0:
        return img
    w, h = img.size
    inner_w = max(1, w - 2 * margin_px)
    inner_h = max(1, h - 2 * margin_px)
    scaled = img.resize((inner_w, inner_h))
    canvas = Image.new('L', (w, h), color=255)
    canvas.paste(scaled, (margin_px, margin_px))
    return canvas


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

    return canvas, to_bitmap_bytes(canvas)
