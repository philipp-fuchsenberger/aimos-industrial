# AIMOS Connectors Package
from .base import AIMOSConnector

CONNECTOR_REGISTRY = {
    "telegram": "Telegram Bot API",
    "email": "E-Mail (IMAP/SMTP)",
    "webhook": "Webhook (HTTP POST)",
    "rest_api": "REST API",
    "dashboard": "Dashboard Chat",
    "voice": "Voice I/O (STT/TTS)",
}
