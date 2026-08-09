import logging
import socket
import time

logger = logging.getLogger(__name__)

# 2000ms window to collect labels for multi-column compositing
MULTI_COLUMN_WINDOW = 2

# ESC!? status byte, bits combine (e.g. 0x0B = ribbon+jam+head).
# pause(0x10)/printing(0x20) are not failures on their own.
STATUS_BITS = {
    0x01: "head_opened",
    0x02: "paper_jam",
    0x04: "out_of_paper",
    0x08: "out_of_ribbon",
    0x10: "pause",
    0x20: "printing",
    0x80: "other_error",
}
STATUS_ERROR_MASK = 0x01 | 0x02 | 0x04 | 0x08 | 0x80


class PrinterStatusError(RuntimeError):
    """Print failed due to a detected printer status, not just a socket error.

    Carries the raw status byte so callers can branch on the specific reason
    (out_of_paper vs paper_jam vs head_opened, ...) instead of only knowing
    "it failed" — e.g. to map to a dedicated confirmPrint status code later.
    """

    def __init__(self, code: int, phase: str):
        self.code = code
        self.phase = phase  # "pre_print" or "post_print"
        super().__init__(f"{phase}: {describe_status(code)}")

# Single-label pixel size -> multi-column strip config.
# tspl_size / tspl_gap: physical dimensions of the full composite strip (fill in when known).
# tspl_x / tspl_y: BITMAP dot offsets (same as single-label entry).
MULTI_COLUMN_SIZES: dict[tuple[int, int], dict] = {
    (280, 130): {
        'cols': 5,
        'gap_px': 20,
        # Physical strip size — measure on real tape and adjust:
        'tspl_size': '78 mm,12.7 mm',
        'tspl_gap': '3 mm,0',
        'tspl_x': 10,
        'tspl_y': 10,
        'margin_px': 0,  # not yet tuned for this tape — raise if edge bleed is seen
    },
    (106, 106): {
        'cols': 6,
        'gap_px': 32,       # ERT-AM009X009Z1 tape: 9mm sticker + 2.57mm gap = 11.57mm pitch.
                            # At 300dpi (12 dots/mm per TSPL doc), pitch = 139 dots.
                            # gap_px = pitch - label_w(106) = 32. (30 was too small -> pitch
                            # short by ~5 dots/col, drifting labels left of their die-cut cell,
                            # worse each column -> QR/text past cell edge on later labels.)
        'tspl_size': '71.95 mm,9.5 mm', # width: measured total (SIZE clips print buffer if too small —
                                        # 66.85mm content-only broke printing, so declare full length).
        'tspl_gap': '2.57 mm,0',
        'tspl_x': 21,
        'tspl_y': 16,       # real cause of earlier bottom-clip was a stale gap-sensor calibration
                            # (see calibrate.py), not this offset. Post-calibration, plenty of
                            # spare blank space below the text (real usable height > assumed), but
                            # tspl_y=6 left ~no top margin -> QR top edge clipped. Pushed down;
                            # re-check photo, tune further if top/bottom margin still uneven.
        'margin_px': 7,     # ~0.58mm blank buffer on each side of content, absorbs drift so
                            # bitmap edge doesn't bleed past the die-cut cell into the gap.
                            # Tune up if black edge bleed still seen, down if content looks
                            # too shrunk on real prints.
    },
}


def select_print_command(data):
    """
    Create first part of command for printer
    :param data: dict received from mqtt
    :return:
    """
    msg = None
    if "width" in data and "height" in data:
        _width = int(data["width"])
        _height = int(data["height"])
        if _width == 256 and _height == 100:
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 22 mm,10 mm\r\nGAP 2 mm,0\r\nCLS\r\nBITMAP 2,9,32,100,0,"
        elif _width == 584 and _height == 280:
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 50 mm,25 mm\r\nGAP 3 mm,0\r\nCLS\r\nBITMAP 3,7,73,280,0,"
        elif _width == 824 and _height == 320:
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 70 mm,30 mm\r\nGAP 3 mm,0\r\nCLS\r\nBITMAP 1,17,103,320,0,"
        elif _width == 584 and _height == 340:
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 50 mm,30 mm\r\nGAP 3 mm,0\r\nCLS\r\nBITMAP 3,30,73,340,0,"
        elif _width == 880 and _height == 280:
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 76.2 mm,25.4 mm\r\nGAP 3 mm,0\r\nCLS\r\nBITMAP 10,10,110,280,0,"
        elif _width == 280 and _height == 130:
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 25.4 mm,12.7 mm\r\nGAP 3 mm,0\r\nCLS\r\nBITMAP 10,10,35,130,0,"
        elif _width == 104 and _height == 100:
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 9 mm,9 mm\r\nGAP 2.8 mm,0\r\nCLS\r\nBITMAP 1,6,13,100,0,"
        elif _width == 528 and _height == 340:
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 45 mm,30 mm\r\nGAP 3 mm,0\r\nCLS\r\nBITMAP 2,30,66,340,0,"
    return msg


def describe_status(code: int) -> str:
    if code == 0:
        return "normal"
    flags = [name for bit, name in STATUS_BITS.items() if code & bit]
    return ",".join(flags) if flags else f"unknown(0x{code:02x})"


def query_status(address: str, port: int, timeout: float = 2.0) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((address, port))
        s.sendall(b"\x1b!?")
        data = s.recv(1)
    finally:
        s.close()
    if not data:
        raise OSError("no response to status query")
    return data[0]


def send_and_verify(address: str, port: int, cmd: bytes, label_count: int, timeout: float = 2.0) -> None:
    """Send a print command on one TCP session, checking printer status before and after.

    Raw TCP send succeeding does not mean the label actually printed (e.g. paper runs
    out mid-job) — polling ESC!? on the same connection catches that instead of
    silently reporting success.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((address, port))

        s.sendall(b"\x1b!?")
        pre = s.recv(1)[0]
        if pre & STATUS_ERROR_MASK:
            raise PrinterStatusError(pre, "pre_print")

        s.sendall(cmd)

        code = 0x20  # assume still printing until polled otherwise
        deadline = time.monotonic() + 2.0 + 1.5 * label_count
        while time.monotonic() < deadline:
            time.sleep(0.25)
            s.sendall(b"\x1b!?")
            code = s.recv(1)[0]
            if not (code & 0x20):
                break
        if code & STATUS_ERROR_MASK:
            raise PrinterStatusError(code, "post_print")
        if code & 0x20:
            logger.warning("TSC status poll timed out while still printing (label_count=%d)", label_count)
    finally:
        s.close()


def poll_health(address: str, port: int, interval: float = 60.0) -> None:
    """Background loop logging printer status changes, independent of print jobs.

    TODO: log-only means status changes are only visible to someone tailing the
    log file — no live sink. Wire up a real notification path once one exists
    (e.g. MQTT publish to a status topic on the already-open broker connection,
    or POST to an MSS endpoint if one gets added).
    """
    last = None
    while True:
        try:
            code = query_status(address, port)
            if code != last:
                level = logger.warning if code & STATUS_ERROR_MASK else logger.info
                level("TSC printer status: %s", describe_status(code))
                last = code
        except OSError as e:
            if last != "unreachable":
                logger.warning("TSC printer unreachable: %s", e)
                last = "unreachable"
        time.sleep(interval)


def print_batch(address: str, port: int, msg_rx: dict, tsc_bitmap: bytes, count: int) -> None:
    cmd_prefix = select_print_command(msg_rx)
    if not cmd_prefix:
        raise ValueError(f"No TSC command for dimensions {msg_rx.get('width')}x{msg_rx.get('height')}")
    cmd = cmd_prefix.encode() + tsc_bitmap + f"\r\nPRINT 1,{count}\r\n".encode()
    send_and_verify(address, port, cmd, label_count=count)


def print_multi_column(address: str, port: int, images: list, bitmaps: list, mc_cfg: dict) -> None:
    label_w, label_h = images[0].size
    w_bytes = (label_w + 7) // 8
    pitch = label_w + mc_cfg['gap_px']

    # One BITMAP command per label, each at its own physical x offset — no
    # Python-side canvas compositing, so no dependency on gap_px matching
    # the real print pitch (only tspl_x/pitch need to match the tape).
    bitmap_cmds = [
        f"BITMAP {mc_cfg['tspl_x'] + i * pitch},{mc_cfg['tspl_y']},{w_bytes},{label_h},0,".encode()
        + bmp
        for i, bmp in enumerate(bitmaps)
    ]
    tspl_prefix = (
        f"DENSITY 13\r\nSPEED 1\r\n"
        f"SIZE {mc_cfg['tspl_size']}\r\n"
        f"GAP {mc_cfg['tspl_gap']}\r\n"
        f"CLS\r\n"
    ).encode()
    cmd = tspl_prefix + b"\r\n".join(bitmap_cmds) + b"\r\nPRINT 1,1\r\n"

    send_and_verify(address, port, cmd, label_count=len(bitmaps))
