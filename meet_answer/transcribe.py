"""Speech to text with the speech model chosen in the Rflow profile (sst.engines), loaded once and again only when the
choice changes. A cloud model falls back on Parakeet when the provider fails, as in Rflow. Also reads WAV files, for
`python -m meet_answer --file`."""
import logging
import threading
import time
import wave
from pathlib import Path

import numpy as np

from meet_answer.rflow import RflowSetup
from sst.engines import DEFAULT_MODEL, SPEECH_MODELS, load_engine, usable
from sst.engines.cloud import CLOUD, REMOTE
from sst.live.contracts import RATE

log = logging.getLogger(__name__)


class NoSpeechModel(RuntimeError):
    pass


def choice(setup: RflowSetup, speech_model: str = "") -> tuple:
    """What the engine depends on: the model (`speech_model`, else the profile's) and, for a cloud model or an own server,
    its language, model, key and address."""
    s, keys = setup.settings, setup.gateway
    key = usable(speech_model or s.speech_model, keys)
    if key in CLOUD:
        return key, s.speech_language, keys.key_for(key), s.speech_cloud_models.get(key) or CLOUD[key].models[0], None
    if key in REMOTE:
        address, api_key = keys.speech_server()
        return key, s.speech_language, api_key, s.speech_server_model, address
    return key, s.speech_language, "", "", None


class Transcriber:
    def __init__(self, load=load_engine, speech_model: str = ""):
        self._load, self.speech_model = load, speech_model
        self._engine = None
        self._choice: tuple = ()
        self._local = None  # Parakeet: chosen, or for a cloud model that couldn't help
        self._lock = threading.Lock()  # the chosen engine
        self._local_lock = threading.Lock()  # Parakeet, also asked for by a cloud engine while it transcribes

    def prepare(self, setup: RflowSetup) -> str:
        """Load the chosen engine now (at the start, so the first question doesn't wait for it); its name."""
        with self._lock:
            return self._ready(setup).name

    def transcribe(self, audio: np.ndarray, setup: RflowSetup) -> str:
        with self._lock:
            engine = self._ready(setup)
        if hasattr(engine, "words"):
            engine.words = list(setup.settings.vocabulary)  # the user's names and terms, as Rflow gives them
        t0 = time.perf_counter()
        text = engine.transcribe(audio, RATE).strip()
        log.info("Transcribed %.1f s with %s in %.2f s (%d words)", len(audio) / RATE, engine.name,
                 time.perf_counter() - t0, len(text.split()))
        return text

    def _ready(self, setup: RflowSetup):
        wanted = choice(setup, self.speech_model)
        if not wanted[0]:
            raise NoSpeechModel("No speech model is set up: choose one in Rflow (Speech).")
        if self._engine is None or wanted != self._choice:
            t0 = time.perf_counter()
            key, language, api_key, model, address = wanted
            if key in REMOTE:
                self._engine = self._load(key, language, api_key, model, self._fallback, address)
            elif key == DEFAULT_MODEL:
                self._engine = self._fallback()
            else:
                self._engine = self._load(key, language)
                self._local = None  # another model runs on this computer: Parakeet's memory can go
            self._choice = wanted
            log.info("Speech model %s ready in %.1f s", key, time.perf_counter() - t0)
        return self._engine

    def _fallback(self):
        with self._local_lock:
            if self._local is None:
                if not SPEECH_MODELS[DEFAULT_MODEL].installed():
                    raise RuntimeError("Parakeet isn't downloaded to take over")
                self._local = self._load(DEFAULT_MODEL)
            return self._local


def read_wav(path: Path) -> np.ndarray:
    """A 16-bit or 32-bit PCM WAV file as float32 mono at RATE (resampled if it has another rate)."""
    with wave.open(str(path), "rb") as f:
        channels, width, rate, raw = f.getnchannels(), f.getsampwidth(), f.getframerate(), f.readframes(f.getnframes())
    if width not in (2, 4):
        raise ValueError(f"{path.name}: only 16-bit or 32-bit PCM WAV files can be read")
    audio = np.frombuffer(raw, dtype=np.int16 if width == 2 else np.int32).astype(np.float32) / (2 ** (8 * width - 1))
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(audio):
        positions = np.arange(int(len(audio) * RATE / rate)) * (rate / RATE)
        audio = np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)
    return audio
