#!/usr/bin/python3
# -*- coding: utf-8 -*-
import logging
import ssl
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import paho.mqtt.client as mqtt_client

from printer_service import mqtt, runtime, test_preview, worker
from printer_service.config import AppConfig, get_config

LOG_FORMAT = '%(asctime)s %(levelname)-8s %(message)s'
LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'

logging.basicConfig(level=logging.DEBUG, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
logger = logging.getLogger(__name__)


def _configure_file_logging(config: AppConfig) -> None:
    if not config.logging.path:
        return

    log_path = Path(config.logging.path)
    if log_path.suffix == "":
        log_path = log_path / "printer-tsc.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=config.logging.max_bytes,
        backupCount=config.logging.backup_count,
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(config.logging.level)
    logger.info("File logging enabled: %s", log_path)


if __name__ == "__main__":
    logger.info("Label printer service starting")
    config = get_config()

    if config:
        _configure_file_logging(config)
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
