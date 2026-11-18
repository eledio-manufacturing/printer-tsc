import logging
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from brother_ql.models import ModelsManager
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# Resolved from the package location, not the working directory, so the service
# starts regardless of where it is launched from (systemd WorkingDirectory etc.).
CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'config.yaml'


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
    # INFO + 10 MB: DEBUG logged every MQTT payload and status poll, and at 1 MB the
    # rotated files covered only hours -- too little history, too many SD card writes.
    level: str = "INFO"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


class TscPrinterConfig(BaseModel):
    type: Literal['tsc']
    address: str
    port: int
    health_poll_interval: float | None = 60.0  # None/0 disables the background health poll


class BrotherQlPrinterConfig(BaseModel):
    type: Literal['brother_ql']
    identifier: str
    model: str

    @field_validator('model')
    @classmethod
    def _known_model(cls, v: str) -> str:
        # brother_ql only fails on an unknown model at the first print, with an empty
        # exception message (BrotherQLUnknownModel()) -- catch it at startup instead.
        known = list(ModelsManager().identifiers())
        if v not in known:
            raise ValueError(f"unknown Brother model {v!r}, expected one of {known}")
        return v


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
        with open(CONFIG_PATH, 'r') as f:
            return AppConfig.model_validate(yaml.safe_load(f))
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        return None
