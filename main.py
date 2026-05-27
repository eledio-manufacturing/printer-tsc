#!/usr/bin/python3
# -*- coding: utf-8 -*-
import json
import logging
import os
import socket
import ssl
import tempfile
import time
import yaml

import paho.mqtt.client as mqtt_client
import requests

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)
from PIL import Image

import usb.core

from brother_ql.conversion import convert
from brother_ql.backends import backend_factory, guess_backend
from brother_ql.raster import BrotherQLRaster


BROTHER_LABEL_SIZES = {
    (165, 566): "17x54",
    (165, 956): "17x87",
    (202, 202): "23x23",
    (306, 425): "29x42",
    (306, 991): "29x90",
    (413, 991): "39x90",
    (425, 495): "39x48",
    (578, 271): "52x29",
    (696, 271): "62x29",
    (696, 1109): "62x100",
    (1164, 526): "102x51",
    (1164, 1660): "102x152",
}


def select_brother_label_size(width: int, height: int) -> str:
    label = BROTHER_LABEL_SIZES.get((width, height))
    if label is None:
        raise ValueError(f"No Brother label matches pixel size {width}x{height}")
    return label


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


def message_handle(client, user_data, message):
    msg_rx = json.loads(message.payload.decode("utf-8"))
    logger.debug("MQTT message received: %s", msg_rx)

    auth = (user_data['erp']['auth']["username"], user_data['erp']['auth']["password"])
    print_id = None

    # get label
    if msg_rx["url"].startswith("https://"):
        auth = (user_data['mss']['auth']["username"], user_data['mss']['auth']["password"])
        r = requests.get(msg_rx["url"], auth=auth)
        if 'printHistoryId' in msg_rx:
            print_id = msg_rx['printHistoryId']
    else:
        auth = (user_data['erp']['auth']["username"], user_data['erp']['auth']["password"])
        r = requests.get(user_data['erp']['hostname'] + msg_rx["url"], auth=auth)
    fd, path = tempfile.mkstemp()
    pcx_path = path + ".pcx"
    try:
        os.write(fd, r.content)
        os.close(fd)
        label_img = Image.open(path).convert('1')
        label_img.save(pcx_path)
        label = Image.open(pcx_path)
        label.load()
    finally:
        for f in (path, pcx_path):
            if os.path.exists(f):
                os.remove(f)

    printer_type = user_data['printer'].get('type', 'tsc')
    if printer_type == 'brother_ql':
        # Handle Brother QL printer
        model = user_data['printer'].get('model', 'QL-500')
        identifier = user_data['printer'].get('identifier')
        if not identifier:
            logger.error("Brother QL printer identifier not configured")
            return
        try:
            label_size = select_brother_label_size(int(msg_rx["width"]), int(msg_rx["height"]))
            qlr = BrotherQLRaster(model)
            instructions = convert(qlr, [label_img], label_size, cut=False)
            if identifier.startswith('usb://'):
                dev = usb.core.find(idVendor=0x04f9)
                if dev:
                    dev.reset()
            selected_backend = guess_backend(identifier)
            be = backend_factory(selected_backend)
            printer = be['backend_class'](identifier)
            try:
                printer.write(instructions)
            finally:
                printer._dispose()
            if print_id:
                requests.post(url=f'https://mss.eledio.com/api/confirmPrint?id={print_id}&status=1', auth=auth)
        except Exception as e:
            logger.error("Error printing to Brother QL: %s", e)
            if print_id:
                requests.post(url=f'https://mss.eledio.com/api/confirmPrint?id={print_id}&status=2', auth=auth)
    else:
        # Handle TSC printer
        if 'address' not in user_data['printer'] or 'port' not in user_data['printer']:
            logger.error("TSC printer address or port not configured")
            return
        # create command part
        cmd_first_part = select_print_command(msg_rx)
        if cmd_first_part:
            cmd_last_part = "\r\nPRINT 1,1\r\n"
            cmd = cmd_first_part.encode() + label.tobytes() + cmd_last_part.encode()
            # create socket and send it to the printer
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((user_data['printer']['address'], user_data['printer']['port']))
                s.send(cmd)
                s.close()
                if print_id:
                    requests.post(url=f'https://mss.eledio.com/api/confirmPrint?id={print_id}&status=1', auth=auth)
            except Exception as e:
                logger.error("Error printing to TSC: %s", e)
                if print_id:
                    requests.post(url=f'https://mss.eledio.com/api/confirmPrint?id={print_id}&status=2', auth=auth)


def on_connect(mqtt_client, obj, flags, rc):
    if rc == 0:
        logger.info("MQTT: connected")
        mqtt_client.subscribe(obj['mqtt']['topic'])
    else:
        retry_time = 2
        while rc != 0:
            time.sleep(retry_time)
            try:
                rc = mqtt_client.reconnect()
            except Exception as e:
                rc = 1
                retry_time = 5


def on_subscribe(mqtt_client, obj, flags, rc):
    logger.info("MQTT: subscribed")


def get_config():
    try:
        f = open('config/config.yaml', 'r')
        data = yaml.safe_load(f)
        return data
    except:
        return None


if __name__ == "__main__":
    logger.info("TSC label printer service starting")
    mqtt = mqtt_client.Client()
    config = get_config()

    if config:
        mqtt.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        mqtt.username_pw_set(username=config['mqtt']['auth']['username'], password=config['mqtt']['auth']['password'])
        mqtt.connect(host=config['mqtt']['hostname'], port=config['mqtt']['port'])
        mqtt.on_connect = on_connect
        mqtt.on_subscribe = on_subscribe
        mqtt.message_callback_add(sub=config['mqtt']['topic'], callback=message_handle)
        mqtt.user_data_set(userdata=config)

        mqtt.loop_forever()
    else:
        logger.error("Configuration was not provided")
