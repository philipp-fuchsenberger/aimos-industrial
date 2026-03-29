#!/usr/bin/env python3
"""
AIMOS Voice Listener — Always-On Speech-to-Queue Relay
========================================================
Listens on a configured audio input device, detects speech via webrtcvad,
transcribes via Whisper, and enqueues the text as pending_message for a
specific agent (default: voice_agent).

Zero VRAM in idle — Whisper model loaded only when speech is detected.
Designed to run permanently alongside shared_listener.py.

Usage:
  python scripts/voice_listener.py                          # default: agent=voice_agent, device=default
  python scripts/voice_listener.py --agent voice_agent --device 7  # USB speaker
  python scripts/voice_listener.py --device 9                # PipeWire tunnel

Audio Tunneling (remote mic via WLAN):
  On remote machine:  pactl load-module module-native-protocol-tcp
  On AIMOS server:    pactl load-module module-tunnel-source server=REMOTE_IP
  The tunnel appears as a new PipeWire/PulseAudio input device.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("AIMOS.voice_listener")

# ── DB ───────────────────────────────────────────────────────────────────────

def _enqueue_message(agent_name: str, content: str):
    """Write transcribed speech to pending_messages."""
    import psycopg2
    import psycopg2.extras
    from core.config import Config
    try:
        conn = psycopg2.connect(
            host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
            user=Config.PG_USER, password=Config.PG_PASSWORD,
            connect_timeout=5,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pending_messages (agent_name, sender_id, content, kind) "
            "VALUES (%s, 0, %s, 'voice_local') RETURNING id",
            (agent_name, content),
        )
        msg_id = cur.fetchone()[0]
        cur.execute("UPDATE agents SET wake_up_needed=TRUE WHERE name=%s", (agent_name,))
        conn.close()
        log.info(f"Enqueued #{msg_id} for '{agent_name}' [voice_local]: {content[:60]}")
        return msg_id
    except Exception as exc:
        log.error(f"Enqueue failed: {exc}")
        return None


# ── TTS Cooldown (CR-095: prevents mic picking up speaker output) ─────────────
_tts_cooldown_until: float = 0.0
_TTS_COOLDOWN_SECS = float(os.getenv("VOICE_TTS_COOLDOWN", "4.0"))

# ── TTS Response ─────────────────────────────────────────────────────────────

def _check_and_speak_response(agent_name: str, output_device: int | None, output_sr: int) -> bool:
    """Check DB for agent responses and speak them via TTS. Returns True if something was spoken."""
    import psycopg2
    import psycopg2.extras
    from core.config import Config
    try:
        conn = psycopg2.connect(
            host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
            user=Config.PG_USER, password=Config.PG_PASSWORD,
            connect_timeout=5, cursor_factory=psycopg2.extras.RealDictCursor,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT id, content FROM aimos_chat_histories "
            "WHERE agent_name=%s AND role='assistant' AND metadata->>'spoken' IS NULL "
            "ORDER BY id ASC LIMIT 1",
            (agent_name,),
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return False

        text = row["content"]
        msg_id = row["id"]

        cur.execute(
            "UPDATE aimos_chat_histories SET metadata = metadata || '{\"spoken\": true}'::jsonb WHERE id=%s",
            (msg_id,),
        )
        conn.close()

        if not text or len(text.strip()) < 2:
            return False

        log.info(f"[TTS] Speaking response ({len(text)} chars)")

        try:
            from core.skills.voice_io import VoiceIOSkill
            vio = VoiceIOSkill()
            vio._output_device = output_device
            vio._output_sr = output_sr
            vio._speak_sync(text[:500])  # keep TTS short for snappy responses
            # CR-095: Set cooldown so mic ignores own TTS output
            global _tts_cooldown_until
            _tts_cooldown_until = time.monotonic() + _TTS_COOLDOWN_SECS
            log.info(f"[TTS] Cooldown active for {_TTS_COOLDOWN_SECS}s (loop prevention)")
            return True
        except Exception as exc:
            log.warning(f"[TTS] Failed: {exc}")
            return False

    except Exception as exc:
        log.debug(f"Response check failed: {exc}")
        return False


# ── Main Listen Loop ─────────────────────────────────────────────────────────

def _load_agent_secrets(agent_name: str):
    """Load agent env_secrets from DB into os.environ (for Piper, Whisper, etc.)."""
    import json
    import psycopg2
    import psycopg2.extras
    from core.config import Config
    try:
        conn = psycopg2.connect(
            host=Config.PG_HOST, port=Config.PG_PORT, dbname=Config.PG_DB,
            user=Config.PG_USER, password=Config.PG_PASSWORD,
            cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute("SELECT env_secrets FROM agents WHERE name=%s", (agent_name,))
        row = cur.fetchone()
        conn.close()
        if row and row["env_secrets"]:
            sec = row["env_secrets"]
            if isinstance(sec, str):
                sec = json.loads(sec)
            for k, v in sec.items():
                if k and v and isinstance(v, str):
                    os.environ.setdefault(k, v)
            log.info(f"Loaded {len(sec)} secrets for '{agent_name}'")
    except Exception as exc:
        log.warning(f"Failed to load secrets: {exc}")


def listen_loop(agent_name: str, input_device: int | None, output_device: int | None,
                output_sr: int, shutdown_event):
    """Continuous VAD → Whisper → DB loop."""
    import numpy as np
    import sounddevice as sd

    # Load agent secrets (PIPER_EXECUTABLE, PIPER_VOICE_PATH, etc.)
    _load_agent_secrets(agent_name)

    try:
        import webrtcvad
    except ImportError:
        log.error("webrtcvad not installed — pip install webrtcvad")
        return

    sr = 16000  # webrtcvad requires 8/16/32/48 kHz
    frame_ms = 30
    frame_n = sr * frame_ms // 1000  # 480 samples

    vad_aggressiveness = int(os.getenv("VOICE_VAD_AGGRESSIVENESS", "3"))
    silence_ratio = float(os.getenv("VOICE_VAD_SILENCE_RATIO", "0.85"))
    window_sec = float(os.getenv("VOICE_VAD_WINDOW_SEC", "0.6"))
    pre_roll_ms = int(os.getenv("VOICE_PRE_ROLL_MS", "500"))
    min_speech_sec = float(os.getenv("VOICE_MIN_SPEECH_SEC", "0.4"))
    min_words = int(os.getenv("VOICE_MIN_WORDS", "2"))
    record_limit = float(os.getenv("VOICE_RECORD_LIMIT", "30"))

    ring_size = int(window_sec * 1000 / frame_ms)
    pre_n = int(pre_roll_ms / frame_ms)
    min_frames = int(min_speech_sec * 1000 / frame_ms)
    max_frames = int(record_limit * 1000 / frame_ms)

    vad = webrtcvad.Vad(vad_aggressiveness)
    whisper_model = None

    log.info(f"Voice listener active: agent={agent_name}, input_device={input_device}, "
             f"vad_aggr={vad_aggressiveness}")

    while not shutdown_event.is_set():
        # Phase 1: Listen for speech
        ring = deque(maxlen=ring_size)
        pre_roll = deque(maxlen=pre_n)
        speech_started = False
        frames = []
        total = 0

        try:
            with sd.InputStream(samplerate=sr, channels=1, dtype="int16",
                                device=input_device) as stream:
                while total < max_frames and not shutdown_event.is_set():
                    data, _ = stream.read(frame_n)
                    total += 1

                    try:
                        is_speech = vad.is_speech(data.tobytes(), sr)
                    except Exception:
                        is_speech = False

                    if not speech_started:
                        pre_roll.append(data.copy())
                        # CR-095: Don't trigger during post-TTS cooldown
                        if is_speech and time.monotonic() >= _tts_cooldown_until:
                            speech_started = True
                            frames.extend(pre_roll)
                            ring.clear()
                    else:
                        frames.append(data.copy())
                        ring.append(is_speech)
                        if len(ring) == ring_size:
                            silence = 1.0 - (sum(ring) / ring_size)
                            if silence >= silence_ratio:
                                break
        except Exception as exc:
            log.error(f"Audio stream error: {exc}")
            time.sleep(5)
            continue

        if len(frames) < min_frames:
            # Check for pending TTS responses while idle
            _check_and_speak_response(agent_name, output_device, output_sr)
            continue

        # Phase 2: Transcribe with Whisper
        log.info(f"Speech detected ({len(frames)} frames) — transcribing...")

        if whisper_model is None:
            try:
                from faster_whisper import WhisperModel
                from core.config import Config
                model_name = Config.WHISPER_MODEL
                log.info(f"Loading Whisper {model_name}...")
                whisper_model = WhisperModel(model_name, device="cuda", compute_type="float16")
            except Exception as exc:
                log.error(f"Whisper load failed: {exc}")
                continue

        audio_int16 = np.concatenate(frames).flatten()
        audio_float = audio_int16.astype(np.float32) / 32768.0

        try:
            language = os.getenv("WHISPER_LANGUAGE", "de")
            beam_size = int(os.getenv("WHISPER_BEAM_SIZE", "3"))
            segments, _ = whisper_model.transcribe(audio_float, language=language, beam_size=beam_size)
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            log.error(f"Transcription failed: {exc}")
            continue

        if len(text.split()) < min_words:
            log.debug(f"Too short, ignored: '{text}'")
            continue

        log.info(f"Transcribed: '{text[:80]}'")

        # Phase 3: Enqueue to DB
        _enqueue_message(agent_name, f"[Sprachbefehl] {text}")

        # Phase 4: Poll aggressively for TTS response (max 30s, check every 1s)
        for _ in range(30):
            time.sleep(1)
            if shutdown_event.is_set():
                break
            if _check_and_speak_response(agent_name, output_device, output_sr):
                break  # spoken — back to listening


# ── Entry ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AIMOS Voice Listener")
    parser.add_argument("--agent", default="voice_agent", help="Agent to send voice input to (default: voice_agent)")
    parser.add_argument("--device", type=int, default=None, help="Audio input device index (default: system default)")
    parser.add_argument("--output-device", type=int, default=None, help="Audio output device for TTS")
    parser.add_argument("--output-sr", type=int, default=44100, help="Output sample rate (default: 44100)")
    args = parser.parse_args()

    log.info("=" * 50)
    log.info("  AIMOS Voice Listener v4.2.0")
    log.info(f"  Agent: {args.agent}")
    log.info(f"  Input device: {args.device or 'default'}")
    log.info(f"  Output device: {args.output_device or 'default'}")
    log.info("=" * 50)

    import threading
    shutdown = threading.Event()

    def _signal_handler(sig, frame):
        log.info("Shutdown signal received")
        shutdown.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    listen_loop(args.agent, args.device, args.output_device, args.output_sr, shutdown)
    log.info("Voice listener stopped.")


if __name__ == "__main__":
    main()
