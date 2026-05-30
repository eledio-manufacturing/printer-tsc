#!/usr/bin/python3
# -*- coding: utf-8 -*-
import json
import logging
import os
import queue
import socket
import ssl
import tempfile
import threading
import time
from typing import Annotated, Literal, Union
import yaml

import paho.mqtt.client as mqtt_client
import requests
from PIL import Image
import usb.core
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from brother_ql.raster import BrotherQLRaster
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


class Auth(BaseModel):
    username: str
    password: str


class MqttConfig(BaseModel):
    hostname: str
    port: int
    topic: str
    auth: Auth


class ServiceConfig(BaseModel):
    hostname: str
    auth: Auth


class TscPrinterConfig(BaseModel):
    type: Literal['tsc']
    address: str
    port: int


class BrotherQlPrinterConfig(BaseModel):
    type: Literal['brother_ql']
    identifier: str
    model: str


PrinterConfig = Annotated[
    Union[TscPrinterConfig, BrotherQlPrinterConfig],
    Field(discriminator='type'),
]


class AppConfig(BaseModel):
    mqtt: MqttConfig
    erp: ServiceConfig
    mss: ServiceConfig
    printer: PrinterConfig


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

# Image cache: (url, width, height) -> (monotonic_time, (label_img_L, tsc_bitmap_bytes))
CACHE_TTL = 300  # seconds
_cache: dict[tuple, tuple[float, tuple]] = {}
_cache_lock = threading.Lock()

# 50ms window to accumulate identical labels into a single print command
BATCH_WINDOW = 0.05

_config: AppConfig | None = None
print_queue: queue.Queue = queue.Queue()


def cache_get(key: tuple) -> tuple | None:
    with _cache_lock:
        entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def cache_set(key: tuple, value: tuple) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)


def select_brother_label_size(width: int, height: int) -> tuple[str, bool]:
    result = BROTHER_LABEL_SIZES.get((width, height)) or BROTHER_LABEL_SIZES.get((height, width))
    if result is None:
        raise ValueError(f"No Brother label matches pixel size {width}x{height}")
    return result


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
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 9 mm,9 mm\r\nGAP 3 mm,0\r\nCLS\r\nBITMAP 1,6,13,100,0,"
        elif _width == 528 and _height == 340:
            msg = "DENSITY 13\r\nSPEED 1\r\nSIZE 45 mm,30 mm\r\nGAP 3 mm,0\r\nCLS\r\nBITMAP 2,30,66,340,0,"
    return msg


def _fetch_image(url: str) -> tuple[Image.Image, bytes]:
    """Download and convert image. Returns (L-mode PIL Image, 1-bit bitmap bytes for TSC)."""
    cfg = _config
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


def _confirm_all(print_ids: list, status: int) -> None:
    cfg = _config
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
    cfg = _config

    if count > 1:
        logger.debug("Printing batch of %d identical labels (url=%s)", count, job['url'])

    if isinstance(cfg.printer, BrotherQlPrinterConfig):
        try:
            upscale_target = DPI_600_UPSCALE.get(label_img.size)
            if upscale_target:
                logger.debug("Upscaling image %s -> %s for 600dpi", label_img.size, upscale_target)
                label_img = label_img.resize(upscale_target, Image.Resampling.LANCZOS)
            label_size, dpi_600 = select_brother_label_size(*label_img.size)
            qlr = BrotherQLRaster(cfg.printer.model)
            instructions = convert(qlr, [label_img] * count, label_size, cut=False, dpi_600=dpi_600, compress=False, hq=True)
            if cfg.printer.identifier.startswith('usb://'):
                dev = usb.core.find(idVendor=0x04f9)
                if dev:
                    dev.reset()
            send(instructions, printer_identifier=cfg.printer.identifier, blocking=False)
            _confirm_all(print_ids, status=1)
        except Exception as e:
            logger.error("Error printing to Brother QL: %s", e)
            _confirm_all(print_ids, status=2)
    else:
        cmd_prefix = select_print_command(job['msg_rx'])
        if cmd_prefix:
            cmd = cmd_prefix.encode() + tsc_bitmap + f"\r\nPRINT 1,{count}\r\n".encode()
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((cfg.printer.address, cfg.printer.port))
                s.send(cmd)
                s.close()
                _confirm_all(print_ids, status=1)
            except Exception as e:
                logger.error("Error printing to TSC: %s", e)
                _confirm_all(print_ids, status=2)
        else:
            logger.error("No TSC command for dimensions %sx%s", job['width'], job['height'])
            _confirm_all(print_ids, status=2)


def print_worker() -> None:
    while True:
        try:
            job = print_queue.get()
            batch = [job]
            deadline = time.monotonic() + BATCH_WINDOW
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    next_job = print_queue.get(timeout=remaining)
                    if (next_job['url'] == job['url']
                            and next_job['width'] == job['width']
                            and next_job['height'] == job['height']):
                        batch.append(next_job)
                    else:
                        # Different label — re-queue and flush current batch
                        print_queue.put(next_job)
                        break
                except queue.Empty:
                    break
            _print_batch(batch)
        except Exception as e:
            logger.error("Print worker unhandled error: %s", e)


def message_handle(client, config: AppConfig, message):
    msg_rx = json.loads(message.payload.decode("utf-8"))
    logger.debug("MQTT message received: %s", msg_rx)

    url = msg_rx["url"]
    width = int(msg_rx.get("width", 0))
    height = int(msg_rx.get("height", 0))
    print_id = msg_rx.get('printHistoryId') if url.startswith("https://") else None

    key = (url, width, height)
    image_data = cache_get(key)
    if image_data is None:
        try:
            image_data = _fetch_image(url)
            cache_set(key, image_data)
        except Exception as e:
            logger.error("Failed to fetch/convert image %s: %s", url, e)
            if print_id:
                auth = (config.mss.auth.username, config.mss.auth.password)
                try:
                    requests.post(
                        url=f'{config.mss.hostname}/api/confirmPrint?id={print_id}&status=2',
                        auth=auth,
                    )
                except Exception as ce:
                    logger.error("confirmPrint failed: %s", ce)
            return
    else:
        logger.debug("Cache hit for %s %dx%d", url, width, height)

    print_queue.put({
        'url': url,
        'width': width,
        'height': height,
        'print_id': print_id,
        'msg_rx': msg_rx,
        'image_data': image_data,
    })


def on_connect(client, obj: AppConfig, connect_flags, reason_code, properties):
    if reason_code.value == 0:
        logger.info("MQTT: connected")
        client.subscribe(obj.mqtt.topic)
    else:
        logger.error("MQTT: connection failed (reason %s)", reason_code)


def on_disconnect(client, obj, disconnect_flags, reason_code, properties):
    if reason_code.value != 0:
        logger.warning("MQTT: unexpected disconnect, will reconnect automatically")


def on_subscribe(client, obj, mid, reason_codes, properties):
    logger.info("MQTT: subscribed")


def get_config() -> AppConfig | None:
    try:
        with open('config/config.yaml', 'r') as f:
            return AppConfig.model_validate(yaml.safe_load(f))
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        return None


if __name__ == "__main__":
    logger.info("TSC label printer service starting")
    config = get_config()

    if config:
        _config = config

        worker = threading.Thread(target=print_worker, daemon=True)
        worker.start()

        mqtt = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        mqtt.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        mqtt.username_pw_set(username=config.mqtt.auth.username, password=config.mqtt.auth.password)
        mqtt.connect(host=config.mqtt.hostname, port=config.mqtt.port)
        mqtt.on_connect = on_connect
        mqtt.on_disconnect = on_disconnect
        mqtt.on_subscribe = on_subscribe
        mqtt.message_callback_add(sub=config.mqtt.topic, callback=message_handle)
        mqtt.user_data_set(userdata=config)
        mqtt.reconnect_delay_set(min_delay=1, max_delay=30)

        mqtt.loop_forever()
    else:
        logger.error("Configuration was not provided")
