#!/usr/bin/python3
# -*- coding: utf-8 -*-
import json
import logging
import os
import socket
import ssl
import tempfile
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


def message_handle(client, config: AppConfig, message):
    msg_rx = json.loads(message.payload.decode("utf-8"))
    logger.debug("MQTT message received: %s", msg_rx)

    print_id = None

    if msg_rx["url"].startswith("https://"):
        auth = (config.mss.auth.username, config.mss.auth.password)
        r = requests.get(msg_rx["url"], auth=auth)
        if 'printHistoryId' in msg_rx:
            print_id = msg_rx['printHistoryId']
    else:
        auth = (config.erp.auth.username, config.erp.auth.password)
        r = requests.get(config.erp.hostname + msg_rx["url"], auth=auth)

    fd, path = tempfile.mkstemp()
    pcx_path = path + ".pcx"
    try:
        os.write(fd, r.content)
        os.close(fd)
        label_img = Image.open(path).convert('L')
        label_img.convert('1', dither=Image.Dither.NONE).save(pcx_path)
        label = Image.open(pcx_path)
        label.load()
    finally:
        for f in (path, pcx_path):
            if os.path.exists(f):
                os.remove(f)

    if isinstance(config.printer, BrotherQlPrinterConfig):
        try:
            upscale_target = DPI_600_UPSCALE.get(label_img.size)
            if upscale_target:
                logger.debug("Upscaling image %s -> %s for 600dpi", label_img.size, upscale_target)
                label_img = label_img.resize(upscale_target, Image.Resampling.LANCZOS)
            label_size, dpi_600 = select_brother_label_size(*label_img.size)
            qlr = BrotherQLRaster(config.printer.model)
            instructions = convert(qlr, [label_img], label_size, cut=False, dpi_600=dpi_600, compress=False, hq=True)
            if config.printer.identifier.startswith('usb://'):
                dev = usb.core.find(idVendor=0x04f9)
                if dev:
                    dev.reset()
            send(instructions, printer_identifier=config.printer.identifier)
            if print_id:
                requests.post(url=f'{config.mss.hostname}/api/confirmPrint?id={print_id}&status=1', auth=auth)
        except Exception as e:
            logger.error("Error printing to Brother QL: %s", e)
            if print_id:
                requests.post(url=f'{config.mss.hostname}/api/confirmPrint?id={print_id}&status=2', auth=auth)
    else:
        cmd_first_part = select_print_command(msg_rx)
        if cmd_first_part:
            cmd_last_part = "\r\nPRINT 1,1\r\n"
            cmd = cmd_first_part.encode() + label.tobytes() + cmd_last_part.encode()
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((config.printer.address, config.printer.port))
                s.send(cmd)
                s.close()
                if print_id:
                    requests.post(url=f'{config.mss.hostname}/api/confirmPrint?id={print_id}&status=1', auth=auth)
            except Exception as e:
                logger.error("Error printing to TSC: %s", e)
                if print_id:
                    requests.post(url=f'{config.mss.hostname}/api/confirmPrint?id={print_id}&status=2', auth=auth)


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
    mqtt = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    config = get_config()

    if config:
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
