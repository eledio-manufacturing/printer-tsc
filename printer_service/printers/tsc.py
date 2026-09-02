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

# Chunk size / inter-chunk delay for sending BITMAP payloads. Label data can
# intermittently shift a few pixels partway through printing -- confirmed via
# repeated identical test prints to be content-independent (same bytes at the
# same offsets printed clean on one run, corrupted on another) and to only
# show up once the printer was moved from a switched connection to a direct
# Pi<->printer Ethernet link. Working theory: a switch's store-and-forward
# buffering naturally paces/spaces frames; without one, the host can burst
# data faster than the printer's embedded receive/print pipeline drains it,
# and something downstream of TCP (so invisible to Ethernet-level error
# counters) intermittently drops or misaligns bytes under that burst.
# Pacing the writes in software emulates what the switch used to do.
SEND_CHUNK_SIZE = 512
SEND_CHUNK_DELAY = 0.05

# Single-label pixel size -> multi-column strip config.
# tspl_size / tspl_gap: physical dimensions of the full composite strip (fill in when known).
# tspl_x / tspl_y: BITMAP dot offsets (same as single-label entry).
MULTI_COLUMN_SIZES: dict[tuple[int, int], dict] = {
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
    """Send a print command and verify it printed cleanly.

    A status error (jam, out of paper, head opened, ...) or the printer being
    unreachable both need a physical fix -- raise immediately instead of
    blocking the whole print queue waiting for it to clear, so the caller can
    give up the label and report the failure (with reason) to MSS right away.
    """
    _send_and_verify_once(address, port, cmd, label_count, timeout)


def _send_throttled(s: socket.socket, data: bytes, chunk_size: int = SEND_CHUNK_SIZE, delay: float = SEND_CHUNK_DELAY) -> None:
    """Send data in small chunks with a pause between them.

    A plain sendall() of the whole BITMAP payload lets the OS/NIC burst it out
    as fast as the link allows -- some TSC print servers can't keep up and
    corrupt/misalign the data partway through. Pacing the writes keeps the
    printer's receive buffer from being overrun.
    """
    for i in range(0, len(data), chunk_size):
        s.sendall(data[i:i + chunk_size])
        if delay and i + chunk_size < len(data):
            time.sleep(delay)


def _send_and_verify_once(address: str, port: int, cmd: bytes, label_count: int, timeout: float = 2.0) -> None:
    """Send a print command, then poll status on fresh connections until done.

    Raw TCP send succeeding does not mean the label actually printed (e.g. paper runs
    out mid-job) — polling ESC!? catches that instead of silently reporting success.
    The polls use their own short-lived connections (like query_status) rather than
    reusing the send socket: TSC firmware can close/reset that socket at any point
    once it starts printing, so treating that socket as long-lived for the poll
    loop made an ordinary mid-print disconnect look like a fatal error.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((address, port))

        s.sendall(b"\x1b!?")
        pre_data = s.recv(1)
        if not pre_data:
            raise OSError("no response to pre-print status query")
        pre = pre_data[0]
        if pre & STATUS_ERROR_MASK:
            raise PrinterStatusError(pre, "pre_print")

        _send_throttled(s, cmd)
    finally:
        s.close()

    code = 0x20  # assume still printing until polled otherwise
    deadline = time.monotonic() + 2.0 + 1.5 * label_count
    while time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            code = query_status(address, port, timeout=timeout)
        except OSError:
            # Refused / no reply / reset are all normal while the head is
            # actively printing -- keep polling until the outer deadline
            # instead of treating an ordinary mid-print disconnect as fatal.
            continue
        if not (code & 0x20):
            break
    if code & STATUS_ERROR_MASK:
        raise PrinterStatusError(code, "post_print")
    if code & 0x20:
        logger.warning("TSC status poll timed out while still printing (label_count=%d)", label_count)


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


def print_batch(address: str, port: int, msg_rx: dict, tsc_bitmap: bytes, count: int, on_printed=None) -> None:
    cmd_prefix = select_print_command(msg_rx)
    if not cmd_prefix:
        raise ValueError(f"No TSC command for dimensions {msg_rx.get('width')}x{msg_rx.get('height')}")
    # One PRINT 1,1 job per label (own status verification) instead of a single
    # PRINT 1,count job, so a jam partway through leaves earlier labels confirmed
    # instead of failing the whole batch -- same reasoning as brother.print_labels.
    cmd = cmd_prefix.encode() + tsc_bitmap + b"\r\nPRINT 1,1\r\n"
    for i in range(count):
        send_and_verify(address, port, cmd, label_count=1)
        if on_printed:
            on_printed(i)


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
