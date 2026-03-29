import asyncio
import logging
import os
import subprocess
import tempfile
import threading
import numpy as np
from collections import deque
from pathlib import Path
from typing import Optional

from .base import BaseSkill

log = logging.getLogger("VoiceIOSkill")

def _resolve_path(raw: str) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(raw)))) if raw else raw


def _resample_audio(data: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
    """Resample mono float32 audio to a new sample rate.

    Uses scipy.signal.resample_poly when available (best quality, handles any
    ratio).  Falls back to numpy linear interpolation so the module works without
    scipy installed.
    """
    if from_sr == to_sr:
        return data
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(to_sr, from_sr)
        return resample_poly(data, to_sr // g, from_sr // g).astype(np.float32)
    except ImportError:
        pass
    # Fallback: linear interpolation — no extra dependency required.
    n_out = max(1, int(round(len(data) * to_sr / from_sr)))
    xp = np.linspace(0.0, 1.0, len(data), dtype=np.float64)
    x  = np.linspace(0.0, 1.0, n_out,    dtype=np.float64)
    return np.interp(x, xp, data).astype(np.float32)

class VoiceIOSkill(BaseSkill):
    name = "voice_io"
    display_name = "Voice I/O (Mic/Speaker)"

    def __init__(self) -> None:
        from core.config import Config
        self._whisper_model_name = Config.WHISPER_MODEL
        # CR-131: Whisper always on CPU — Qwen 3.5:27b uses ~24GB VRAM, no room for Whisper GPU
        self._whisper_device     = os.getenv("WHISPER_DEVICE", "cpu")
        self._whisper_compute    = os.getenv("WHISPER_COMPUTE_TYPE", "float16")
        self._whisper_language   = os.getenv("WHISPER_LANGUAGE", "de")
        self._whisper_beam_size  = int(os.getenv("WHISPER_BEAM_SIZE", "3"))

        self._piper_exe        = _resolve_path(os.getenv("PIPER_EXECUTABLE", "/home/philipp/AIMOS/models/piper/piper"))
        self._piper_voice_path = _resolve_path(os.getenv("PIPER_VOICE_PATH", ""))

        # webrtcvad: 0 = least aggressive (allows more speech),
        #            3 = most aggressive (filters noise strictly)
        self._vad_aggressiveness = int(os.getenv("VOICE_VAD_AGGRESSIVENESS", "3"))
        # Frame size for webrtcvad. Must be 10, 20, or 30 ms.
        self._vad_frame_ms       = int(os.getenv("VOICE_VAD_FRAME_MS", "30"))
        # End-of-speech: trigger when this fraction of the ring buffer is silent.
        # 0.85 = 85 % of the last _vad_window_sec must be non-speech.
        self._vad_silence_ratio  = float(os.getenv("VOICE_VAD_SILENCE_RATIO", "0.85"))
        # Duration of the ring-buffer lookback window (seconds).
        # 0.6s = aggressive cutoff for low-latency conversation.
        self._vad_window_sec     = float(os.getenv("VOICE_VAD_WINDOW_SEC", "0.6"))
        # Pre-roll: frames captured before speech onset to keep first syllable.
        self._pre_roll_ms        = int(os.getenv("VOICE_PRE_ROLL_MS", "300"))
        # Hard maximum recording length (seconds).
        self._record_limit       = float(os.getenv("VOICE_RECORD_LIMIT", "60"))
        # Minimum speech duration before we even attempt transcription.
        self._min_speech_sec     = float(os.getenv("VOICE_MIN_SPEECH_SEC", "0.4"))
        # Minimum word count in the transcript — filters "ähm", single grunts, etc.
        self._min_words          = int(os.getenv("VOICE_MIN_WORDS", "2"))

        # webrtcvad only supports 8 / 16 / 32 / 48 kHz — fix to 16 kHz.
        self._sample_rate        = 16000

        _input_idx = os.getenv("AUDIO_INPUT_INDEX", "").strip()
        self._input_device: Optional[int] = int(_input_idx) if _input_idx else None

        _output_idx = os.getenv("AUDIO_OUTPUT_INDEX", "").strip()
        self._output_device: Optional[int] = int(_output_idx) if _output_idx else None

        # Target sample rate for the output device (Pipewire/Jabra: 44100 Hz).
        # Piper voices typically produce 22050 Hz; _resample_audio() bridges the gap.
        self._output_sr = int(os.getenv("AUDIO_OUTPUT_SAMPLERATE", "44100"))

        # Inter-sentence silence (ms). Played via sd.sleep() after each sentence
        # with echo-guard OFF so the user can interrupt before the next sentence.
        self._tts_sentence_pause_ms = int(os.getenv("TTS_SENTENCE_PAUSE_MS", "400"))

        self._model      = None
        self._backend    = None
        # Echo-guard: True while TTS is playing.
        # _listen_sync drains the mic but discards audio when this is set.
        self._is_speaking = False
        # Set by _listen_sync at speech onset; checked by tts_worker before
        # each sentence to detect child interruptions during inter-sentence gaps.
        self._speech_onset = threading.Event()
        # Set by stop() to break blocking audio loops on shutdown.
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signal all audio loops to exit and release sounddevice resources.

        Safe to call from any thread. Idempotent.
        """
        self._stop_event.set()
        self._is_speaking = False
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    def __del__(self) -> None:
        self.stop()

    def is_available(self) -> bool:
        try:
            import sounddevice
            return True
        except ImportError:
            return False

    async def preload(self) -> None:
        """Eagerly loads the Whisper model into VRAM and logs audio device info.

        Call once at startup so the first listen() has zero model-load latency.
        Runs in a thread executor to keep the event loop responsive.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._reset_portaudio)
        await loop.run_in_executor(None, self._load_model)
        await loop.run_in_executor(None, self._log_audio_devices)

    def _reset_portaudio(self) -> None:
        """Terminate and re-initialize PortAudio to evict zombie clients.

        On systems where speech-dispatcher-dummy or another PortAudio consumer
        holds exclusive access, a clean terminate/initialize cycle forces them
        to release the device before we open our streams.
        """
        try:
            import sounddevice as sd
            sd._terminate()
            sd._initialize()
            log.info("[Audio] PortAudio reset complete (terminate → initialize)")
        except Exception as exc:
            log.warning(f"[Audio] PortAudio reset failed (non-fatal): {exc}")

    def _log_audio_devices(self) -> None:
        try:
            import sounddevice as sd
            out_info   = sd.query_devices(self._output_device, "output")
            in_info    = sd.query_devices(self._input_device,  "input")
            # Resolve the actual integer index sounddevice resolved to
            all_devs   = sd.query_devices()
            out_idx    = self._output_device if self._output_device is not None \
                         else sd.default.device[1]
            in_idx     = self._input_device  if self._input_device  is not None \
                         else sd.default.device[0]
            out_label  = (f"Device Index: {out_idx} | Name: {out_info['name']} "
                          f"| Target SR: {self._output_sr} Hz (stereo)")
            in_label   = f"Device Index: {in_idx}  | Name: {in_info['name']}"
            print(f"  [Audio Out] {out_label}")
            print(f"  [Audio In]  {in_label}")
            log.info(f"[Audio Out] {out_label}")
            log.info(f"[Audio In]  {in_label}")
        except Exception as exc:
            log.warning(f"[Audio] device query failed: {exc}")

    async def listen(self) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._listen_sync)

    async def speak(self, text: str) -> None:
        if not text.strip():
            return
        if not self._piper_voice_path:
            log.warning("speak() called but PIPER_VOICE_PATH is not set – no audio output.")
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._speak_sync, text)

    async def transcribe_file(self, path: str) -> str:
        """Transcribes an audio file (OGG, WAV, MP3, …) using the Whisper model.

        Lazy-loads the Whisper model if not already in VRAM — so this works even
        when --voice is not active (e.g. Telegram-only voice pipeline).

        Args:
            path: Filesystem path to the audio file.

        Returns:
            Transcribed text, or empty string if the file is too short / silent.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._transcribe_file_sync, path)

    def _transcribe_file_sync(self, path: str) -> str:
        """Synchronous transcription of an audio file. Called in a thread executor."""
        self._load_model()
        # CR-116: language=None → Whisper auto-detects the language
        segments, info = self._model.transcribe(
            path,
            language=self._whisper_language if self._whisper_language else None,
            beam_size=self._whisper_beam_size,
        )
        seg_list = list(segments)
        text = " ".join(s.text.strip() for s in seg_list).strip()

        # CR-215e: Confidence check — flag low-confidence transcriptions
        lang_prob = getattr(info, 'language_probability', 1.0) if info else 1.0
        if seg_list:
            avg_logprob = sum(getattr(s, 'avg_logprob', 0) for s in seg_list) / len(seg_list)
        else:
            avg_logprob = 0
        # avg_logprob < -1.0 or language_probability < 0.7 → unreliable
        if lang_prob < 0.7 or avg_logprob < -1.0:
            text = (f"[Transcription uncertain (confidence: {lang_prob:.0%})] {text}\n"
                    f"[Note: This voice message was hard to understand. "
                    f"Please ask the customer to confirm the transcription.]")
            log.warning(f"[STT-File] Low confidence: lang_prob={lang_prob:.2f} avg_logprob={avg_logprob:.2f}")

        log.info(f"[STT-File] {path!r} → {repr(text)[:80]}")
        return text

    async def synthesize_to_file(self, text: str, output_path: str) -> str:
        """Synthesizes text via Piper TTS and writes the WAV to a specific file.

        Does NOT play audio — for use in Telegram voice response pipeline.
        PIPER_VOICE_PATH must be set; raises RuntimeError otherwise.

        Args:
            text:        Text to synthesize.
            output_path: Destination WAV file path (will be created/overwritten).

        Returns:
            The resolved output_path string.

        Raises:
            RuntimeError: If PIPER_VOICE_PATH is not set or Piper fails.
        """
        if not self._piper_voice_path:
            raise RuntimeError(
                "synthesize_to_file() requires PIPER_VOICE_PATH to be set in .env"
            )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._synthesize_file_sync, text, output_path
        )

    def _synthesize_file_sync(self, text: str, output_path: str) -> str:
        """Synchronous Piper TTS to file. Called in a thread executor."""
        cmd = [
            self._piper_exe,
            "--model", self._piper_voice_path,
            "--output_file", output_path,
        ]
        result = subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Piper failed (code {result.returncode}): {err or '(no stderr)'}"
            )
        log.info(f"[TTS-File] synthesized {len(text)} chars → {output_path!r}")
        return output_path

    def _load_model(self):
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel
        # CR-117: Try GPU first, fall back to CPU if VRAM is full (Qwen 3.5:27b uses ~23GB)
        device = self._whisper_device
        compute = "float16" if device == "cuda" else "int8"
        try:
            log.info(f"Loading Whisper {self._whisper_model_name} on {device}...")
            self._model = WhisperModel(
                self._whisper_model_name,
                device=device,
                compute_type=compute,
            )
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower() or "CUDA" in str(exc):
                log.warning(f"Whisper GPU failed ({exc}), falling back to CPU")
                self._model = WhisperModel(
                    self._whisper_model_name,
                    device="cpu",
                    compute_type="int8",
                )
            else:
                raise
        self._backend = "faster"
        return self._model

    def _speak_sync(self, text: str) -> None:
        # Clear any stale speech-onset flag from the user's own input turn.
        # Only a speech event that occurs DURING this playback is a real interruption.
        self._speech_onset.clear()

        tmp = Path(tempfile.mktemp(suffix=".wav"))
        try:
            cmd = [self._piper_exe, "--model", self._piper_voice_path, "--output_file", str(tmp)]
            result = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=60)  # CR-192
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"Piper exited with code {result.returncode}.\n"
                    f"  Command: {' '.join(cmd)}\n"
                    f"  Stderr:  {err or '(empty)'}"
                )

            import sounddevice as sd
            import soundfile as sf

            # 1. Read Piper output (typically 22050 Hz mono).
            data, src_sr = sf.read(str(tmp), dtype="float32", always_2d=False)
            if data.ndim > 1:
                data = data.mean(axis=1).astype(np.float32)   # stereo → mono first

            # 2. Resample to device native rate (default 44100 Hz for Pipewire/Jabra).
            #    Pipewire silently mutes streams whose sample rate doesn't match the
            #    sink's configured rate.
            data = _resample_audio(data, src_sr, self._output_sr)

            # 3. Mono → Stereo: many sound servers (Pipewire, PulseAudio) mute
            #    mono streams on multi-channel output devices.
            stereo = np.ascontiguousarray(
                np.column_stack([data, data]), dtype=np.float32
            )  # shape (N, 2)

            # 4. Prefix silence – prevents Jabra/USB hardware from clipping the
            #    first phoneme while the device wakes up (150ms).
            prefix = np.zeros((int(self._output_sr * 0.15), 2), dtype=np.float32)
            speech_audio = np.concatenate([prefix, stereo], axis=0)

            # 5. Peak booster (×2, hard-clipped to [-1, 1]).
            #    Guards against Piper producing very quiet output that is below
            #    the hardware or mixer volume threshold.
            speech_audio = np.clip(speech_audio * 2.0, -1.0, 1.0).astype(np.float32)

            # 6. Buffer sanity check — log first 10 samples + peak so we can
            #    distinguish TTS-silent from playback-silent in the output.
            peak = float(np.abs(speech_audio).max())
            preview = speech_audio[int(self._output_sr * 0.15):int(self._output_sr * 0.15) + 10, 0]
            log.info(
                f"[Audio] play  dev={self._output_device}  sr={self._output_sr}  "
                f"shape={speech_audio.shape}  peak={peak:.4f}  "
                f"samples={np.round(preview, 4).tolist()}"
            )
            if peak < 1e-6:
                log.warning("[Audio] peak is near-zero — Piper produced silence or resampling failed")

            # 7. Hard-blocking playback — Python blocks until PortAudio confirms
            #    the last sample has left the buffer.  No threading, no races.
            self._is_speaking = True
            try:
                sd.play(speech_audio, samplerate=self._output_sr,
                        device=self._output_device, blocking=True)
            finally:
                self._is_speaking = False

            # 8. Inter-sentence pause — echo-guard OFF.
            #    sd.sleep() keeps PortAudio callbacks alive (no silent drop).
            #    During this window _listen_sync can fire _speech_onset.
            if self._tts_sentence_pause_ms > 0:
                sd.sleep(self._tts_sentence_pause_ms)

        finally:
            tmp.unlink(missing_ok=True)

    def _listen_sync(self) -> str:
        """webrtcvad-based speech capture with ring-buffer end-of-speech detection.

        Pipeline:
          1. Pre-roll buffer  – keeps the last _pre_roll_ms of audio so the
                               first syllable is not lost when VAD triggers.
          2. Speech onset     – first frame classified as speech starts recording.
          3. Ring buffer      – sliding window of VAD decisions over the last
                               _vad_window_sec seconds.  Recording stops when
                               >= _vad_silence_ratio of that window is silent.
          4. Echo-guard       – frames captured while _is_speaking (TTS active)
                               are silently discarded; the mic stays open so no
                               buffer overflow occurs on the sounddevice side.
          5. Validation       – transcription is discarded if audio is too short
                               or the transcript has fewer than _min_words words.

        Returns "" (empty) for filtered utterances; the caller loops back.
        """
        import sounddevice as sd
        import webrtcvad

        self._load_model()

        sr         = self._sample_rate          # always 16 000 Hz
        frame_ms   = self._vad_frame_ms         # 30 ms
        frame_n    = sr * frame_ms // 1000      # 480 samples per frame

        # Ring buffer: how many frames cover the lookback window?
        ring_size   = int(self._vad_window_sec * 1000 / frame_ms)   # 50 frames
        pre_n       = int(self._pre_roll_ms / frame_ms)              # 10 frames
        min_frames  = int(self._min_speech_sec * 1000 / frame_ms)    # 14 frames
        max_frames  = int(self._record_limit   * 1000 / frame_ms)

        vad        = webrtcvad.Vad(self._vad_aggressiveness)
        ring       = deque(maxlen=ring_size)   # bool: True = speech
        pre_roll   = deque(maxlen=pre_n)       # raw int16 ndarrays

        # Reset interruption flag for this listening cycle.
        self._speech_onset.clear()
        speech_started = False
        frames: list[np.ndarray] = []
        total = 0

        print("\r🎤 Ich warte...   ", end="", flush=True)

        with sd.InputStream(
            samplerate=sr,
            channels=1,
            dtype="int16",
            device=self._input_device,
        ) as stream:
            while total < max_frames:
                if self._stop_event.is_set():
                    return ""
                data, _ = stream.read(frame_n)
                total += 1

                # ── Echo-guard ────────────────────────────────────────────────
                # Leila is speaking → drain audio silently, do not process.
                if self._is_speaking:
                    speech_started = False
                    frames.clear()
                    ring.clear()
                    pre_roll.clear()
                    continue

                # ── VAD classification ────────────────────────────────────────
                try:
                    is_speech = vad.is_speech(data.tobytes(), sr)
                except Exception:
                    is_speech = False

                if not speech_started:
                    pre_roll.append(data.copy())
                    if is_speech:
                        print("\r👂 Ich höre...    ", end="", flush=True)
                        speech_started = True
                        self._speech_onset.set()   # signal tts_worker to stop queued sentences
                        # Flush pre-roll into frames so first syllable is captured.
                        frames.extend(pre_roll)
                        ring.clear()
                else:
                    frames.append(data.copy())
                    ring.append(is_speech)

                    # ── End-of-speech check ───────────────────────────────────
                    # Only evaluate once the ring buffer has filled at least once.
                    if len(ring) == ring_size:
                        silence_ratio = 1.0 - (sum(ring) / ring_size)
                        if silence_ratio >= self._vad_silence_ratio:
                            break  # sustained silence confirmed

        # ── Validation ────────────────────────────────────────────────────────
        if len(frames) < min_frames:
            print("\r🎤 (Zu kurz, ignoriert)          ")
            return ""

        print("\r🔍 Denke...                       ", end="", flush=True)

        audio_int16 = np.concatenate(frames).flatten()
        audio_float = audio_int16.astype(np.float32) / 32768.0

        segments, _ = self._model.transcribe(
            audio_float,
            language=self._whisper_language,
            beam_size=self._whisper_beam_size,
        )
        text = " ".join(s.text for s in segments).strip()

        if len(text.split()) < self._min_words:
            if text:
                print(f"\r🎤 (Ignoriert: '{text}')          ")
            return ""

        print(f"\r💬 Du: {text}                     ")
        return text
