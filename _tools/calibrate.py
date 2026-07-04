#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""One-off TSC gap-sensor calibration for a given tape.

The printer memorizes its own sensor-calibrated label length/gap in its
own memory, independent of the SIZE/GAP values sent with each print job.
If that memorized length doesn't match the tape currently loaded (e.g.
after switching rolls), the printer silently truncates print jobs past
the old memorized length instead of using whatever SIZE was just sent.

Run this once after loading a new tape, with the approximate label/gap
dimensions for that tape, to make the printer re-feed through the gap
sensor and relearn the real physical length.
"""
import argparse
import socket

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--width-mm', type=float, default=9.0, help='approx label width, mm (default: 9.0)')
    parser.add_argument('--height-mm', type=float, default=9.5, help='approx label height, mm (default: 9.5)')
    parser.add_argument('--gap-mm', type=float, default=2.57, help='approx gap between labels, mm (default: 2.57)')
    parser.add_argument('--address', help='printer IP (default: read from config/config.yaml)')
    parser.add_argument('--port', type=int, help='printer port (default: read from config/config.yaml)')
    args = parser.parse_args()

    address, port = args.address, args.port
    if address is None or port is None:
        with open('../config/config.yaml', 'r') as f:
            printer_cfg = yaml.safe_load(f)['printer']
        address = address or printer_cfg['address']
        port = port or printer_cfg['port']

    cmd = (
        f"SIZE {args.width_mm} mm,{args.height_mm} mm\r\n"
        f"GAP {args.gap_mm} mm,0\r\n"
        f"GAPDETECT\r\n"
    ).encode()

    print(f"Sending calibration to {address}:{port}: {cmd!r}")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((address, port))
    s.send(cmd)
    s.close()
    print("Sent. Printer will feed a few labels through the gap sensor to relearn paper/gap length.")


if __name__ == '__main__':
    main()
