"""Multimodal layer — STT (Speech-to-Text) and TTS (Text-to-Speech).

Converts audio messages to text and vice versa,
enabling transparent Telegram-style communication.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .protocol import Message, MessageType, AgentCard


class MultimodalLayer:
    """Layer that processes audio messages for the agent bus.

    Uses the same providers as Hermes (Whisper, Groq, OpenAI for STT;
    Edge TTS, ElevenLabs, OpenAI for TTS).

    Does not depend on specific external installations — uses whatever
    TTS/STT Hermes already has configured.
    """

    def __init__(self, hermes_home: str | None = None):
        if hermes_home is None:
            hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        self.hermes_home = hermes_home
        self._stt_config = self._load_stt_config()
        self._tts_config = self._load_tts_config()

    def _load_stt_config(self) -> dict:
        """Read STT configuration from Hermes."""
        config_path = os.path.join(self.hermes_home, "config.yaml")
        if os.path.exists(config_path):
            try:
                import yaml
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                return config.get("stt", {"enabled": True, "provider": "local"})
            except ImportError:
                pass
        return {"enabled": True, "provider": "local"}

    def _load_tts_config(self) -> dict:
        """Read TTS configuration from Hermes."""
        config_path = os.path.join(self.hermes_home, "config.yaml")
        if os.path.exists(config_path):
            try:
                import yaml
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                return config.get("tts", {"provider": "edge"})
            except ImportError:
                pass
        return {"provider": "edge"}

    def transcribe(self, audio_path: str) -> str:
        """Convert audio to text using the configured STT.

        Supported formats: .ogg, .wav, .mp3, .m4a, .webm
        """
        provider = self._stt_config.get("provider", "local")

        if provider == "local":
            return self._transcribe_local(audio_path)
        elif provider == "groq":
            return self._transcribe_groq(audio_path)
        elif provider == "openai":
            return self._transcribe_openai(audio_path)
        else:
            return self._transcribe_local(audio_path)

    def _transcribe_local(self, audio_path: str) -> str:
        """Local transcription with faster-whisper."""
        try:
            from faster_whisper import WhisperModel

            model_size = self._stt_config.get("local", {}).get("model", "base")
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            segments, _ = model.transcribe(audio_path, language="en")
            return " ".join(seg.text for seg in segments).strip()
        except ImportError:
            return "[ERROR: faster-whisper not installed. pip install faster-whisper]"
        except Exception as e:
            return f"[Transcription error: {e}]"

    def _transcribe_groq(self, audio_path: str) -> str:
        """Transcription via Groq API."""
        import httpx
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            return "[ERROR: GROQ_API_KEY not set]"
        with open(audio_path, "rb") as f:
            resp = httpx.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (os.path.basename(audio_path), f, "audio/ogg")},
                data={"model": "whisper-large-v3", "language": "en"},
                timeout=30,
            )
        data = resp.json()
        return data.get("text", json.dumps(data))

    def _transcribe_openai(self, audio_path: str) -> str:
        """Transcription via OpenAI Whisper API."""
        import httpx
        api_key = os.environ.get("VOICE_TOOLS_OPENAI_KEY", os.environ.get("OPENAI_API_KEY", ""))
        if not api_key:
            return "[ERROR: OPENAI_API_KEY not set]"
        with open(audio_path, "rb") as f:
            resp = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (os.path.basename(audio_path), f, "audio/ogg")},
                data={"model": "whisper-1", "language": "en"},
                timeout=30,
            )
        data = resp.json()
        return data.get("text", json.dumps(data))

    def synthesize(self, text: str, output_path: str | None = None) -> str:
        """Convert text to audio using the configured TTS.

        Returns:
            Path to the generated audio file.
        """
        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            output_path = tmp.name
            tmp.close()

        provider = self._tts_config.get("provider", "edge")

        if provider == "edge":
            self._synthesize_edge(text, output_path)
        elif provider == "openai":
            self._synthesize_openai(text, output_path)
        elif provider == "elevenlabs":
            self._synthesize_elevenlabs(text, output_path)
        else:
            self._synthesize_edge(text, output_path)

        return output_path

    async def _synthesize_edge_async(self, text: str, output_path: str) -> None:
        """TTS with async Edge TTS."""
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text, "en-US-JennyNeural")
            await communicate.save(output_path)
        except ImportError:
            result = subprocess.run(
                ["edge-tts", "--voice", "en-US-JennyNeural",
                 "--text", text, "--write-media", output_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                with open(output_path, "w") as f:
                    f.write(f"[TTS unavailable: {result.stderr}]")

    def _synthesize_edge(self, text: str, output_path: str) -> None:
        """TTS with Edge TTS (free, natural quality)."""
        try:
            import edge_tts
            import asyncio

            async def _run():
                communicate = edge_tts.Communicate(text, "en-US-JennyNeural")
                await communicate.save(output_path)

            try:
                asyncio.get_running_loop()
                asyncio.ensure_future(self._synthesize_edge_async(text, output_path))
            except RuntimeError:
                asyncio.run(_run())
        except ImportError:
            result = subprocess.run(
                ["edge-tts", "--voice", "en-US-JennyNeural",
                 "--text", text, "--write-media", output_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                with open(output_path, "w") as f:
                    f.write(f"[TTS unavailable: {result.stderr}]")

    def _synthesize_openai(self, text: str, output_path: str) -> None:
        """TTS with OpenAI."""
        import httpx
        api_key = os.environ.get("VOICE_TOOLS_OPENAI_KEY", os.environ.get("OPENAI_API_KEY", ""))
        if not api_key:
            return
        resp = httpx.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "tts-1",
                "voice": "alloy",
                "input": text,
                "response_format": "mp3",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(resp.content)

    def _synthesize_elevenlabs(self, text: str, output_path: str) -> None:
        """TTS with ElevenLabs."""
        import httpx
        api_key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not api_key:
            return
        resp = httpx.post(
            "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM",
            headers={
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": api_key,
            },
            json={
                "text": text,
                "model_id": "eleven_monolingual_v1",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.5},
            },
            timeout=30,
        )
        if resp.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(resp.content)

    def process_audio_message(self, msg: Message) -> Message:
        """Process an audio message: transcribe and return a text message."""
        audio_path = msg.payload if isinstance(msg.payload, str) else ""
        if not audio_path or not os.path.exists(audio_path):
            return Message(
                type=MessageType.ERROR,
                source=msg.target,
                target=msg.source,
                payload="Audio file not found",
                reply_to=msg.id,
            )

        text = self.transcribe(audio_path)
        return Message(
            type=MessageType.TEXT,
            source=msg.source,
            target=msg.target,
            payload=text,
            reply_to=msg.id,
            metadata={"original_audio": audio_path, "transcribed": True},
        )

    def wrap_as_audio(self, msg: Message, text: str | None = None) -> Message:
        """Take a text message and add an audio version."""
        source_text = text or (msg.payload if isinstance(msg.payload, str) else msg.payload.get("result", ""))
        if not source_text:
            return msg

        audio_path = self.synthesize(source_text)
        return Message(
            type=MessageType.AUDIO,
            source=msg.source,
            target=msg.target,
            payload={"text": source_text, "audio_path": audio_path},
            reply_to=msg.reply_to,
            metadata={"has_audio": True},
        )
