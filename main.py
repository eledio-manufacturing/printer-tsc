#!/usr/bin/python3
# -*- coding: utf-8 -*-
import logging
import ssl
import threading

import paho.mqtt.client as mqtt_client

from printer_service import mqtt, runtime, test_preview, worker
from printer_service.config import get_config

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    logger.info("Label printer service starting")
    config = get_config()

    if config:
        runtime.config = config

        worker_thread = threading.Thread(target=worker.print_worker, daemon=True)
        worker_thread.start()

        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.username_pw_set(username=config.mqtt.auth.username, password=config.mqtt.auth.password)
        client.connect(host=config.mqtt.hostname, port=config.mqtt.port)
        client.on_connect = mqtt.on_connect
        client.on_disconnect = mqtt.on_disconnect
        client.on_subscribe = mqtt.on_subscribe
        client.message_callback_add(sub=config.mqtt.topic, callback=mqtt.message_handle)
        client.user_data_set(userdata=config)
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        if test_preview.TEST_MODE:
            try:
                import tkinter as tk
            except ImportError:
                logger.error("TEST_MODE requires tkinter (install e.g. python3-tk system package)")
                raise SystemExit(1)

            logger.info("TEST MODE: printed labels will be shown in a preview window instead")
            root = tk.Tk()
            root.withdraw()  # only Toplevel previews are shown, no root window
            root.after(0, test_preview.poll, root)
            client.loop_start()
            root.mainloop()
        else:
            client.loop_forever()
    else:
        logger.error("Configuration was not provided")
