#!/usr/bin/python3
# -*- coding: utf-8 -*-
import json
import os
import socket
import ssl
import tempfile
import time

import paho.mqtt.client as mqtt_client
import requests
from PIL import Image

MQTT_TOPIC = "print"
MQTT_PORT = 8883
MQTT_HOSTNAME = "hostname"
MQTT_AUTH = {'username': "user", 'password': "password"}

ERP_HOSTNAME = "hostname"
ERP_AUTH = (MQTT_AUTH["username"], MQTT_AUTH["password"])

PRINTER_ADDR = "192.168.5.2"
PRINTER_PORT = 9100


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
    print(msg_rx)
    # get label
    r = requests.get(ERP_HOSTNAME + msg_rx["url"], auth=ERP_AUTH)
    # write label to temporally file
    fd, path = tempfile.mkstemp()
    os.write(fd, r.content)
    os.close(fd)
    # convert label to pcx format, open it and remove file
    label_img = Image.open(path).convert('1')
    label_img.save("{}.pcx".format(path))
    label = Image.open("{}.pcx".format(path))
    os.remove(path)
    os.remove("{}.pcx".format(path))

    # create command part
    cmd_first_part = select_print_command(msg_rx)
    if cmd_first_part:
        cmd_last_part = "\r\nPRINT 1,1\r\n"
        cmd = cmd_first_part.encode() + label.tobytes() + cmd_last_part.encode()
        # create socket and send it to the printer
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((PRINTER_ADDR, PRINTER_PORT))
            s.send(cmd)
            s.close()
        except:
            pass


def on_connect(mqtt_client, obj, flags, rc):
    if rc == 0:
        print("subscribe")
        mqtt_client.subscribe(MQTT_TOPIC)
    else:
        retry_time = 2
        while rc != 0:
            time.sleep(retry_time)
            try:
                rc = mqtt_client.reconnect()
            except Exception as e:
                rc = 1
                retry_time = 5


if __name__ == "__main__":
    mqtt = mqtt_client.Client()
    mqtt.tls_set(None, tls_version=ssl.PROTOCOL_TLSv1_2)
    mqtt.username_pw_set(MQTT_AUTH["username"], password=MQTT_AUTH["password"])
    mqtt.connect(MQTT_HOSTNAME, port=MQTT_PORT)
    mqtt.on_connect = on_connect
    mqtt.message_callback_add(MQTT_TOPIC, message_handle)

    mqtt.loop_forever()
