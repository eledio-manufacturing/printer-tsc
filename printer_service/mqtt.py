import json
import logging

import requests

from printer_service import worker
from printer_service.cache import cache_get, cache_set
from printer_service.config import AppConfig
from printer_service.imaging import fetch_image

logger = logging.getLogger(__name__)


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
            image_data = fetch_image(url)
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

    worker.print_queue.put({
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
