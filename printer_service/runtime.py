"""Process-wide runtime state set once at startup by main.py."""
from printer_service.config import AppConfig

config: AppConfig | None = None
