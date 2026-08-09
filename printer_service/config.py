import logging
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, Field

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


class LoggingConfig(BaseModel):
    path: str | None = None
    level: str = "DEBUG"
    max_bytes: int = 1024 * 1024
    backup_count: int = 5


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
    logging: LoggingConfig = LoggingConfig()


def get_config() -> AppConfig | None:
    try:
        with open('config/config.yaml', 'r') as f:
            return AppConfig.model_validate(yaml.safe_load(f))
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        return None
