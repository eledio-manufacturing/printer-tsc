#!/usr/bin/python3
# -*- coding: utf-8 -*-
import fcntl
import logging
import ssl
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import paho.mqtt.client as mqtt_client

from printer_service import mqtt, runtime, test_preview, worker
from printer_service.config import CONFIG_PATH, AppConfig, BrotherQlPrinterConfig, TscPrinterConfig, get_config
from printer_service.printers import brother as brother_printer
from printer_service.printers import tsc as tsc_printer

LOG_FORMAT = '%(asctime)s %(levelname)-8s %(message)s'
LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'

logging.basicConfig(level=logging.DEBUG, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
logger = logging.getLogger(__name__)


def _configure_file_logging(config: AppConfig) -> None:
    # Applies to console logging too: under systemd the console goes to journald,
    # which is on the same SD card as the log file.
    logging.getLogger().setLevel(config.logging.level)
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
    logger.info("File logging enabled: %s", log_path)


def _acquire_instance_lock():
    """Exit if another instance already runs with this config.

    Two instances on one config both consume every MQTT message, so each label was
    printed twice and confirmed twice. The lock sits next to the config file, so it
    covers exactly "same config"; the kernel drops it when the process dies.
    Returns the open lock file -- it must stay referenced for the process lifetime.
    """
    lock_path = CONFIG_PATH.with_name('.printer-tsc.lock')
    try:
        lock_file = open(lock_path, 'w')
    except OSError as e:
        # A guard, not a requirement: an unwritable config dir must not stop printing.
        logger.warning("Cannot create instance lock %s, running without it: %s", lock_path, e)
        return None
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another printer service instance is already running (lock %s held)", lock_path)
        raise SystemExit(1)
    return lock_file


if __name__ == "__main__":
    logger.info("Label printer service starting")
    instance_lock = _acquire_instance_lock()
    config = get_config()

    if config:
        _configure_file_logging(config)
        runtime.config = config

        if isinstance(config.printer, BrotherQlPrinterConfig):
            brother_printer.check_usb_access(config.printer.identifier)

        worker_thread = threading.Thread(target=worker.print_worker, daemon=True)
        worker_thread.start()

        if isinstance(config.printer, TscPrinterConfig) and config.printer.health_poll_interval:
            health_thread = threading.Thread(
                target=tsc_printer.poll_health,
                args=(config.printer.address, config.printer.port, config.printer.health_poll_interval),
                daemon=True,
            )
            health_thread.start()

        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.username_pw_set(username=config.mqtt.auth.username, password=config.mqtt.auth.password)
        # connect_async + retry_first_connection: a broker/network that isn't up yet
        # (e.g. Pi booting faster than the network after a power cut) is retried with
        # the reconnect backoff instead of crashing at startup like connect() does.
        client.connect_async(host=config.mqtt.hostname, port=config.mqtt.port)
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
            client.loop_forever(retry_first_connection=True)
    else:
        logger.error("Configuration was not provided")
        raise SystemExit(1)  # non-zero so systemd Restart=on-failure / monitoring notices
