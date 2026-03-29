"""
AIMOS Telegram Connector — v4.1.0 (Shard)
============================================
Full Telegram I/O connector based on AIMOSConnector interface.

Features:
  - Token validation (_is_valid_token) before any connection attempt
  - Auto-Bootstrap: unknown chat IDs auto-authorized + persisted to DB
  - send_message / send_typing / send_voice via execute() dispatch
  - OGG/OPUS voice pipeline (WAV → ffmpeg → OGG, fallback to send_audio)
  - Polling mode (manual) and send-only mode (Auto-Pilot)
  - DB queue writer for Shared Listener architecture

Usage:
    connector = TelegramConnector(agent_id="neo", config={
        "telegram_token": "123456:ABC...",
        "allowed_chat_ids": [],
        "auto_pilot": False,
    })
    await connector.connect()
    await connector.execute("send_message", {"chat_id": 123, "text": "Hallo"})
    await connector.disconnect()
"""

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from .base import AIMOSConnector
from core.config import Config

logger = logging.getLogger("AIMOS.Telegram")

_MAX_MSG_LEN = 4096
_ALLOWED_DOC_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".odt", ".xlsx", ".xls",
    ".csv", ".zip", ".7z", ".txt", ".md", ".json",
}


class TelegramConnector(AIMOSConnector):
    """Telegram I/O connector using python-telegram-bot (v21+)."""

    def __init__(self, agent_id: str, config: dict):
        super().__init__(agent_id, config)
        self._token: str = config.get(
            "telegram_token", os.getenv("TELEGRAM_BOT_TOKEN", "")
        )
        self._allowed_ids: set[int] = set(config.get("allowed_chat_ids", []))
        self._auto_pilot: bool = config.get("auto_pilot", False)
        self._app = None
        self._polling: bool = False
        self._incoming: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()
        self._bootstrap_lock: asyncio.Lock = asyncio.Lock()

        # Voice download directory
        voice_dir_env = os.getenv("TELEGRAM_VOICE_DIR", "").strip()
        self._voice_dir = Path(voice_dir_env) if voice_dir_env else (
            Path("storage") / "agents" / self.agent_id / "incoming"
        )

    # ══════════════════════════════════════════════════════════════════════════
    #  Token Validation
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_valid_token(token: str) -> bool:
        """Check Telegram bot token format: <digits>:<hash> (hash >= 10 chars)."""
        if not token or ":" not in token:
            return False
        parts = token.split(":", 1)
        return parts[0].isdigit() and len(parts[1]) >= 10

    # ══════════════════════════════════════════════════════════════════════════
    #  AIMOSConnector Interface
    # ══════════════════════════════════════════════════════════════════════════

    async def connect(self):
        """Validate token, initialize bot application (no polling yet)."""
        if not self._token:
            self.logger.error(
                f"[{self.agent_id}] TELEGRAM_BOT_TOKEN is empty. "
                "Check DB (env_secrets) and .env."
            )
            return
        if not self._is_valid_token(self._token):
            self.logger.error(
                f"[{self.agent_id}] Invalid token format: '{self._token[:10]}...' "
                "(expected: <bot_id>:<hash>)"
            )
            return

        try:
            from telegram.ext import Application
            self._app = Application.builder().token(self._token).build()
            await self._app.initialize()
            await self._app.start()
            self.active = True
            self.logger.info(
                f"[{self.agent_id}] Telegram connected (token: {self._token[:10]}...)"
            )
        except ImportError:
            self.logger.error(
                "python-telegram-bot not installed: pip install python-telegram-bot>=21.0"
            )
        except Exception as exc:
            self.logger.error(f"[{self.agent_id}] Telegram connect failed: {exc}")

    async def execute(self, action: str, params: dict | None = None) -> dict:
        """Dispatch Telegram actions with timeout protection.

        Actions: send_message, send_typing, send_voice, send_photo, send_document
        """
        if not self._app or not self.active:
            self.logger.warning(f"[{self.agent_id}] execute({action}) — not connected")
            return {"error": "not connected"}

        params = params or {}

        try:
            import asyncio as _aio
            if action == "send_message":
                return await _aio.wait_for(self._send_message(params["chat_id"], params["text"]), timeout=15)
            elif action == "send_typing":
                return await _aio.wait_for(self._send_typing(params["chat_id"]), timeout=5)
            elif action == "send_voice":
                return await _aio.wait_for(self._send_voice(params["chat_id"], params["file_path"]), timeout=30)
            elif action == "send_photo":
                return await _aio.wait_for(self._send_photo(params["chat_id"], params["photo"], params.get("caption", "")), timeout=30)
            elif action == "send_document":
                return await _aio.wait_for(self._send_document(params["chat_id"], params["file_path"], params.get("caption", "")), timeout=30)
            else:
                return {"error": f"Unknown action: {action}"}
        except asyncio.TimeoutError:
            self.logger.warning(f"[{self.agent_id}] execute({action}) timed out")
            return {"error": f"{action} timed out"}
        else:
            return {"error": f"Unknown action: {action}"}

    async def disconnect(self):
        """Stop polling and shut down the bot cleanly."""
        self._polling = False
        if self._app:
            try:
                if hasattr(self._app, "updater") and self._app.updater.running:
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as exc:
                self.logger.warning(f"[{self.agent_id}] Telegram shutdown warning: {exc}")
            self._app = None
        self.active = False
        self.logger.info(f"[{self.agent_id}] Telegram disconnected")

    # ══════════════════════════════════════════════════════════════════════════
    #  Polling (manual mode — NOT used in Auto-Pilot)
    # ══════════════════════════════════════════════════════════════════════════

    async def start_polling(self):
        """Start long-polling. Disabled when auto_pilot=True."""
        if self._auto_pilot:
            self.logger.info(f"[{self.agent_id}] Auto-Pilot ON — polling disabled")
            return
        if not self._app or not self.active:
            self.logger.error(f"[{self.agent_id}] Cannot poll — not connected")
            return

        from telegram.ext import MessageHandler, filters as tg_filters

        self._app.add_handler(MessageHandler(
            tg_filters.TEXT & ~tg_filters.COMMAND, self._on_message
        ))
        self._app.add_handler(MessageHandler(tg_filters.VOICE, self._on_message))
        self._app.add_handler(MessageHandler(tg_filters.Document.ALL, self._on_message))

        self._voice_dir.mkdir(parents=True, exist_ok=True)

        await self._app.updater.start_polling(drop_pending_updates=True)
        self._polling = True
        self.logger.info(f"[{self.agent_id}] Telegram polling started")

    async def receive(self) -> tuple[int, str, str]:
        """Wait for next incoming message. Returns (chat_id, content, kind)."""
        return await self._incoming.get()

    # ══════════════════════════════════════════════════════════════════════════
    #  Auto-Bootstrap (unknown chat IDs → DB)
    # ══════════════════════════════════════════════════════════════════════════

    async def _authorize_chat(self, chat_id: int, update=None) -> bool:
        """Auto-authorize an unknown chat ID and persist to DB."""
        async with self._bootstrap_lock:
            if chat_id in self._allowed_ids:
                return True

            self._allowed_ids.add(chat_id)
            await asyncio.to_thread(self._persist_chat_id, chat_id)
            self.logger.info(
                f"[{self.agent_id}] Auto-bootstrap: chat_id {chat_id} authorized + persisted"
            )

            if update and update.message:
                try:
                    await update.message.reply_text(
                        "Willkommen! Du bist jetzt als Administrator autorisiert."
                    )
                except Exception:
                    pass
            return True

    def _persist_chat_id(self, chat_id: int) -> None:
        """Save authorized chat_id to agents.config.allowed_chat_ids in PostgreSQL."""
        try:
            import psycopg2
            db = Config.get_db_params()
            conn = psycopg2.connect(
                host=db["host"], port=db["port"],
                dbname=db["database"], user=db["user"],
                password=db["password"], connect_timeout=3,
            )
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE agents SET config = jsonb_set(
                        config, '{allowed_chat_ids}',
                        (SELECT COALESCE(jsonb_agg(DISTINCT val), '[]'::jsonb)
                         FROM jsonb_array_elements(
                             COALESCE(config->'allowed_chat_ids', '[]'::jsonb) || %s::jsonb
                         ) AS val), true
                    ), updated_at=NOW() WHERE name=%s""",
                    (json.dumps([chat_id]), self.agent_id),
                )
            conn.commit()
            conn.close()
            self.logger.info(f"Chat-ID {chat_id} persisted for agent '{self.agent_id}'")
        except Exception as exc:
            self.logger.warning(f"Persist chat_id failed (in-memory auth remains): {exc}")

    # ══════════════════════════════════════════════════════════════════════════
    #  DB Queue Writer (Shared Listener mode)
    # ══════════════════════════════════════════════════════════════════════════

    async def enqueue_to_db(
        self, chat_id: int, content: str,
        kind: str = "text", file_path: str | None = None,
    ) -> int:
        """Write a message to pending_messages for the orchestrator.

        Used by the Shared Listener, NOT by the agent itself.
        """
        import psycopg2
        db = Config.get_db_params()
        conn = psycopg2.connect(
            host=db["host"], port=db["port"],
            dbname=db["database"], user=db["user"],
            password=db["password"], connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pending_messages (agent_name, sender_id, content, kind, file_path) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (self.agent_id, chat_id, content, kind, file_path),
        )
        msg_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE agents SET wake_up_needed=TRUE WHERE name=%s",
            (self.agent_id,),
        )
        conn.commit()
        conn.close()
        self.logger.info(f"[{self.agent_id}] Enqueued #{msg_id} from {chat_id}")
        return msg_id

    # ══════════════════════════════════════════════════════════════════════════
    #  Send Methods
    # ══════════════════════════════════════════════════════════════════════════

    async def _send_message(self, chat_id: int, text: str) -> dict:
        """Send text, auto-splitting at 4096 char Telegram limit."""
        if not text:
            return {"error": "empty text"}
        chunks = self._split_message(text)
        for chunk in chunks:
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=chunk)
            except Exception as exc:
                self.logger.error(f"[{self.agent_id}] send failed to {chat_id}: {exc}")
                return {"error": str(exc)}
        return {"status": "sent", "chunks": len(chunks)}

    async def _send_typing(self, chat_id: int) -> dict:
        """Send 'typing...' indicator."""
        try:
            from telegram.constants import ChatAction
            await self._app.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.TYPING
            )
            return {"status": "typing"}
        except Exception as exc:
            return {"error": str(exc)}

    async def _send_voice(self, chat_id: int, file_path: str) -> dict:
        """Send audio as Telegram voice message.

        Supports .wav and .ogg input:
          .ogg → sent directly as Telegram voice note
          .wav → converted to OGG/OPUS via ffmpeg, fallback to send_audio
        """
        path = Path(file_path)
        if not path.exists():
            return {"error": f"File not found: {path}"}

        tmp_ogg: Optional[Path] = None
        send_path = path

        try:
            if path.suffix.lower() == ".wav":
                tmp_ogg = path.with_suffix(".ogg")
                result = subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(path),
                        "-c:a", "libopus",
                        "-b:a", "32k",
                        str(tmp_ogg),
                    ],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    send_path = tmp_ogg
                else:
                    self.logger.warning(
                        "ffmpeg conversion failed — sending WAV as audio file"
                    )
                    with open(path, "rb") as f:
                        await self._app.bot.send_audio(chat_id=chat_id, audio=f)
                    return {"status": "sent_as_audio", "file": path.name}

            with open(send_path, "rb") as f:
                await self._app.bot.send_voice(chat_id=chat_id, voice=f)
            self.logger.info(f"Telegram → Voice [{chat_id}]: {send_path.name}")
            return {"status": "sent", "file": send_path.name}

        except Exception as exc:
            self.logger.error(f"send_voice failed (chat_id={chat_id}): {exc}")
            return {"error": str(exc)}
        finally:
            if tmp_ogg is not None and tmp_ogg.exists():
                try:
                    tmp_ogg.unlink()
                except Exception:
                    pass

    async def _send_photo(self, chat_id: int, photo: str, caption: str = "") -> dict:
        """Send a photo to Telegram. `photo` can be a URL or local file path."""
        try:
            path = Path(photo)
            if path.exists():
                with open(path, "rb") as f:
                    await self._app.bot.send_photo(
                        chat_id=chat_id, photo=f,
                        caption=caption[:1024] if caption else None,
                    )
                self.logger.info(f"Telegram → Photo [{chat_id}]: {path.name}")
                return {"status": "sent", "type": "file", "file": path.name}
            else:
                # Treat as URL — download first, then send as file object
                import httpx
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(photo)
                    resp.raise_for_status()
                    img_data = resp.content

                tmp = Path(f"/tmp/tg_photo_{chat_id}.jpg")
                tmp.write_bytes(img_data)
                try:
                    with open(tmp, "rb") as f:
                        await self._app.bot.send_photo(
                            chat_id=chat_id, photo=f,
                            caption=caption[:1024] if caption else None,
                        )
                    self.logger.info(f"Telegram → Photo [{chat_id}]: URL → file delivered")
                    return {"status": "sent", "type": "url_downloaded", "size": len(img_data)}
                finally:
                    tmp.unlink(missing_ok=True)
        except Exception as exc:
            self.logger.error(f"send_photo failed (chat_id={chat_id}): {exc}")
            return {"error": str(exc)}

    async def _send_document(self, chat_id: int, file_path: str, caption: str = "") -> dict:
        """Send a document/file to a Telegram chat."""
        try:
            path = Path(file_path)
            if not path.is_file():
                return {"error": f"File not found: {file_path}"}
            with open(path, "rb") as f:
                await self._app.bot.send_document(
                    chat_id=chat_id, document=f,
                    caption=caption[:1024] if caption else None,
                    filename=path.name,
                )
            self.logger.info(f"Telegram → Document [{chat_id}]: {path.name}")
            return {"status": "sent", "type": "document", "file": path.name}
        except Exception as exc:
            self.logger.error(f"send_document failed (chat_id={chat_id}): {exc}")
            return {"error": str(exc)}

    # ══════════════════════════════════════════════════════════════════════════
    #  Message Handler (polling mode)
    # ══════════════════════════════════════════════════════════════════════════

    async def _on_message(self, update, context):
        """Handle incoming text/voice/document messages."""
        if not update.message:
            return

        # Ignore messages from the bot itself (prevents self-talk loop)
        if update.message.from_user and self._app:
            try:
                if update.message.from_user.id == (await self._app.bot.get_me()).id:
                    return
            except Exception:
                pass

        chat_id: int = update.effective_chat.id

        # Auto-bootstrap: authorize unknown chat IDs immediately
        if chat_id not in self._allowed_ids:
            await self._authorize_chat(chat_id, update)

        # Determine kind + content
        if update.message.text:
            content = update.message.text.strip()
            kind = "text"
        elif update.message.voice:
            kind = "voice"
            try:
                self._voice_dir.mkdir(parents=True, exist_ok=True)
                fid = update.message.voice.file_id
                dest = self._voice_dir / f"{fid}.ogg"
                tg_file = await context.bot.get_file(fid)
                await tg_file.download_to_drive(custom_path=str(dest))
                content = str(dest)
            except Exception as exc:
                self.logger.error(f"Voice download failed: {exc}")
                return
        elif update.message.document:
            kind = "document"
            doc = update.message.document
            fname = doc.file_name or f"doc_{doc.file_id}"
            ext = Path(fname).suffix.lower()
            if ext not in _ALLOWED_DOC_EXTENSIONS:
                try:
                    await update.message.reply_text(
                        f"Dateityp '{ext}' nicht unterstuetzt. "
                        f"Erlaubt: {', '.join(sorted(_ALLOWED_DOC_EXTENSIONS))}"
                    )
                except Exception:
                    pass
                return
            try:
                ws = Path(os.getenv(
                    "AIMOS_WORKSPACE",
                    f"storage/agents/{self.agent_id}",
                ))
                ws.mkdir(parents=True, exist_ok=True)
                dest = ws / fname
                if dest.exists():
                    stem, suffix = dest.stem, dest.suffix
                    counter = 1
                    while dest.exists():
                        dest = ws / f"{stem}_{counter}{suffix}"
                        counter += 1
                tg_file = await context.bot.get_file(doc.file_id)
                await tg_file.download_to_drive(custom_path=str(dest))
                caption = update.message.caption or ""
                content = f"[Datei gespeichert: {dest.name}] {caption}".strip()
            except Exception as exc:
                self.logger.error(f"Document download failed: {exc}")
                return
        else:
            return

        if not content:
            return

        self.logger.info(
            f"[{self.agent_id}] Received [{kind}] from {chat_id}: {content[:60]}"
        )
        await self._incoming.put((chat_id, content, kind))

    # ══════════════════════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _split_message(text: str, limit: int = _MAX_MSG_LEN) -> list[str]:
        """Split long text at newlines, respecting Telegram's 4096 char limit."""
        if len(text) <= limit:
            return [text]
        parts: list[str] = []
        while text:
            if len(text) <= limit:
                parts.append(text)
                break
            split_at = text.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit
            parts.append(text[:split_at].rstrip())
            text = text[split_at:].lstrip("\n")
        return [p for p in parts if p]
