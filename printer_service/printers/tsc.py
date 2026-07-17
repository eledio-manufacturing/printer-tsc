import socket

# 2000ms window to collect labels for multi-column compositing
MULTI_COLUMN_WINDOW = 2

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


def send_command(address: str, port: int, cmd: bytes) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((address, port))
    s.sendall(cmd)
    s.close()
