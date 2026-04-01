"""
AIMOS Config — v4.1.0 (Shard)
================================
Zentrale Konfiguration. Lädt Werte aus .env-Dateien (dotenv)
mit sauberen Defaults für das Docker-Netz (aimos-db, aimos-ollama).

Keine Secrets, IPs oder Tokens werden hardcoded.
"""

import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# ── .env Load Order: core/.env (base) → project root .env (override) ─────────

_core_dir = Path(__file__).parent
_root_dir = _core_dir.parent

_core_env = _core_dir / ".env"
_root_env = _root_dir / ".env"

if _core_env.exists():
    load_dotenv(_core_env)
if _root_env.exists():
    load_dotenv(_root_env, override=True)


class Config:
    """Immutable-ish config object — all values resolved once at import time."""

    # ── Database (asyncpg) ────────────────────────────────────────────────
    PG_HOST: str = os.getenv("PG_HOST", "172.28.0.2")
    PG_PORT: int = int(os.getenv("PG_PORT", "5432"))
    PG_DB: str = os.getenv("PG_DB", "aimos")
    PG_USER: str = os.getenv("PG_USER", "n8n_user")
    PG_PASSWORD: str = os.getenv("PG_PASSWORD", "")

    # ── LLM (Ollama) ─────────────────────────────────────────────────────
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "qwen2.5:14b")
    LLM_KEEP_ALIVE: str = os.getenv("LLM_KEEP_ALIVE", "30m")

    # ── Agent Defaults ────────────────────────────────────────────────────
    TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    DEFAULT_NUM_CTX: int = int(os.getenv("LLM_NUM_CTX", "14336"))
    MAX_TOOL_ROUNDS: int = int(os.getenv("MAX_TOOL_ROUNDS", "5"))
    HISTORY_LIMIT: int = int(os.getenv("HISTORY_LIMIT", "50"))
    POLL_INTERVAL: float = float(os.getenv("POLL_INTERVAL", "2.0"))

    # ── Whisper STT (Single Source of Truth) ─────────────────────────────
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "medium")

    # ── Audio Devices (Alfred-Server: Jabra Speak) ──────────────────────
    AUDIO_OUTPUT_DEVICE: int = int(os.getenv("AUDIO_OUTPUT_DEVICE", "9"))
    AUDIO_INPUT_DEVICE: int = int(os.getenv("AUDIO_INPUT_DEVICE", "10"))

    # ── Output Firewall ───────────────────────────────────────────────────
    CLEAN_CJK: bool = os.getenv("CLEAN_CJK", "true").lower() in ("1", "true", "yes")

    @classmethod
    def get_db_params(cls) -> dict[str, Any]:
        """Return a dict ready for asyncpg.connect() / create_pool()."""
        return {
            "host": cls.PG_HOST,
            "port": cls.PG_PORT,
            "database": cls.PG_DB,
            "user": cls.PG_USER,
            "password": cls.PG_PASSWORD,
            "ssl": False,  # Local Docker PostgreSQL, no SSL needed
        }

    @classmethod
    def ollama_url(cls) -> str:
        """Full Ollama chat endpoint."""
        return f"{cls.LLM_BASE_URL}/api/chat"

    def __repr__(self) -> str:
        return (
            f"Config(db={self.PG_HOST}:{self.PG_PORT}/{self.PG_DB}, "
            f"llm={self.LLM_BASE_URL} model={self.LLM_MODEL})"
        )


# ── SecretFilter ──────────────────────────────────────────────────────────────
_SECRET_KEYS = re.compile(
    r"(password|token|secret|api_key|apikey|credential|auth)", re.IGNORECASE
)


class SecretFilter:
    """Removes sensitive keys from dicts before they are logged or printed."""

    @staticmethod
    def redact(d: dict[str, Any]) -> dict[str, Any]:
        """Return a shallow copy with secret values replaced by '***'."""
        return {
            k: "***" if _SECRET_KEYS.search(k) else v
            for k, v in d.items()
        }


class SecretLogFilter(logging.Filter):
    """Logging filter that masks secret values in log record args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):
            record.args = SecretFilter.redact(record.args)
        return True


# ── Log Rotation ──────────────────────────────────────────────────────────────

_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_LOG_BACKUP_COUNT = 5


def make_rotating_handler(
    log_path: str | Path,
    max_bytes: int = _LOG_MAX_BYTES,
    backup_count: int = _LOG_BACKUP_COUNT,
) -> logging.handlers.RotatingFileHandler:
    """Create a RotatingFileHandler with SecretLogFilter pre-attached.

    Args:
        log_path: Path to the log file.
        max_bytes: Max file size before rotation (default: 10MB).
        backup_count: Number of rotated backups to keep (default: 5).
    """
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.addFilter(SecretLogFilter())
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(name)-24s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    return handler
