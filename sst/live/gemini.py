"""Gemini 3.5 Live Translate as a live captions engine (Google's Live API, a WebSocket; the profile's Gemini key).

    setup        {"setup": {"model": ..., "generationConfig": {"responseModalities": ["AUDIO"],
                  "translationConfig": {"targetLanguageCode": "en", "echoTargetLanguage": false}},
                  "inputAudioTranscription": {}, "outputAudioTranscription": {}}}
                 (the transcripts are the setup's own fields, as in the API reference; Google's Live Translate
                 guide shows them in generationConfig, which the server closes with 1007 "Unknown name")
    audio        {"realtimeInput": {"audio": {"data": <base64 PCM16 16 kHz mono>, "mimeType": "audio/pcm;rate=16000"}}}
    answers      serverContent.inputTranscription.text   the words heard, piece by piece
                 serverContent.outputTranscription.text  their translation, piece by piece
                 serverContent.modelTurn                 the translated voice (24 kHz), unused: captions only
                 goAway                                  the connection ends soon: a new one is opened

The model translates continuously, a few seconds behind the speaker. It only produces audio (and bills it), so the
voice is thrown away and its transcript shown. A line ends when the model ends its turn, or after LiveConfig.line_pause_s
without new words, or once a long translation and the words heard are both at a sentence end (one runs ahead of the
other, either way). How far a line trailed the speaker is measured from when the audio last had
a voice in it to when the line's translation was complete, and only when the voice had paused. A connection lives ~10
minutes: before that a new one is opened (frames wait in the queue meanwhile, so no audio is lost). The key is never
logged.
"""
import base64
import contextlib
import json
import logging
import queue
import re
import socket
import threading
import time
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

import numpy as np

from sst.live.contracts import Kind, LiveConfig, LiveEvent

URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
QUEUE_FRAMES = 50  # 5 s of audio: when the network falls further behind, the oldest frames go (captions stay live)
LONG_LINE = 120  # characters: a translation this long ends its line where both texts are at a sentence end
VOICE_LEVEL = 10 ** (-45 / 20)  # a frame louder than -45 dBFS has a voice in it (for measuring the lag only)
VOICE_PAUSE = 0.3  # seconds: the voice has paused, so a translation arriving now is measured from that pause
_SENTENCE_END = re.compile(r"[.!?。！？]\s*$")
_SENTENCES = re.compile(r"[。！？!?]+|\.+(?!\w)")  # each sentence end once ("?!", "..."); "3.5" is none
_CJK = "".join(f"{chr(a)}-{chr(b)}" for a, b in (
    (0x3000, 0x303F), (0x3040, 0x30FF), (0x31F0, 0x31FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF)))
_CJK_GAP = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}?!])")  # Japanese and Chinese put no spaces between words

log = logging.getLogger(__name__)


ADDRESS_WAIT = 2.0  # seconds for each of Google's addresses to answer before the next one is tried


def _connect(url: str):
    from websockets.sync.client import connect
    sock = None if urllib.request.getproxies() else _socket(urlsplit(url).hostname)  # behind a proxy: websockets' way
    # legacy=True: the connection itself, as before websockets 17 (without it, 17 warns that this use will change)
    return connect(url, sock=sock, open_timeout=10, close_timeout=2, max_size=None, legacy=True)


def _socket(host: str, port: int = 443, each: float = ADDRESS_WAIT) -> socket.socket:
    """A connection to the first of the host's addresses that answers. Python tries them in turn and waits out the whole
    timeout on each: from the dev laptop's network the first of Google's eight addresses never answers (measured: 5 s
    timeout there, 0.03 s for the other seven), which made every start wait 10 s and fail once."""
    error: OSError | None = None
    for family, kind, proto, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        sock = socket.socket(family, kind, proto)
        sock.settimeout(each)
        try:
            sock.connect(address)
        except OSError as e:
            sock.close()
            error = e
            continue
        sock.settimeout(None)
        return sock
    raise error or OSError(f"no address for {host}")


class GeminiLiveTranslate:
    """Feed it frames from any thread; it calls `on_event` (from its own threads) with LiveEvents."""

    def __init__(self, key: str, config: LiveConfig, on_event: Callable[[LiveEvent], None], *,
                 connect: Callable = _connect, clock: Callable[[], float] = time.monotonic, lane: str = "system"):
        self.key, self.config, self.on_event, self.lane = key, config, on_event, lane
        self._connect, self._clock = connect, clock
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=QUEUE_FRAMES)
        self._stop = threading.Event()
        self._lock = threading.Lock()  # the line being built: touched by the receiving and the sending thread
        self._source = self._translation = self._language = ""
        self._last_text = 0.0
        self._lag = 0.0  # the line's: how long after the voice paused its latest translated words came (0 = unknown)
        self._voice_at = 0.0  # when the last frame with a voice in it was captured
        self._ws = None
        self._goaway = False
        self._opened = 0.0
        self._closed: str | None = None  # why the server closed the connection, once it did
        self._thread: threading.Thread | None = None
        self.dropped = 0  # frames dropped because the network fell behind

    # -- the caller's side

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-gemini", daemon=True)
        self._thread.start()

    def feed(self, frame: bytes) -> None:
        """A 100 ms frame of 16 kHz mono PCM16. Never blocks the capture: a full queue loses its oldest frame."""
        if _level(frame) >= VOICE_LEVEL:
            self._voice_at = self._clock()
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._frames.get_nowait()
                self.dropped += 1
            with contextlib.suppress(queue.Full):
                self._frames.put_nowait(frame)

    def stop(self, wait: float = 3.0) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(wait)

    # -- the connection

    def _run(self) -> None:
        backoff, failures = 1.0, 0
        while not self._stop.is_set():
            try:
                self._status("Connecting to Google…")
                ws = self._open()
                backoff, failures = 1.0, 0
                self._status("Listening")
                receiver = threading.Thread(target=self._receive, args=(ws,), name="live-gemini-in", daemon=True)
                receiver.start()
                self._send(ws)
                self._finish_line()  # a new connection starts with no memory of this line
                with contextlib.suppress(Exception):
                    ws.close()
                receiver.join(2)
                self._ws = None
            except Exception as e:
                self._ws = None
                if self._stop.is_set():
                    break
                failures += 1
                log.warning("Live captions: %s", self._scrub(_describe(e)))
                if failures == 1 and isinstance(e, TimeoutError):
                    continue  # a first connection that stalls (seen on the dev laptop): again at once, quietly
                message = self._explain(e)
                self.on_event(LiveEvent(Kind.ERROR, message, lane=self.lane))
                if message in GIVE_UP:
                    break  # trying again won't help
                self._status("Reconnecting…")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
        self._finish_line()
        self._status("Stopped")

    def _open(self):
        self._goaway, self._closed = False, None
        ws = self._connect(f"{URL}?key={self.key}")
        self._ws = ws
        ws.send(json.dumps({"setup": {
            "model": f"models/{self.config.model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],  # the model only speaks; its transcript is what's shown
                "translationConfig": {"targetLanguageCode": self.config.target,
                                      "echoTargetLanguage": self.config.echo_target},
            },
            "inputAudioTranscription": {},  # the words heard
            "outputAudioTranscription": {},  # the translation, as text
        }}))
        reply = json.loads(ws.recv(timeout=15))
        if "setupComplete" not in reply:
            raise ConnectionError(f"setup not accepted: {str(reply)[:200]}")
        self._opened = self._clock()
        return ws

    def _send(self, ws) -> None:
        """Stream the frames until it's time for a new connection, the server closes it, or stop()."""
        while not self._stop.is_set():
            if self._closed is not None:
                raise self._closed
            if self._goaway or self._clock() - self._opened > self.config.reconnect_s:
                log.info("Live captions: a new connection (the old one ends soon)")
                return
            try:
                frame = self._frames.get(timeout=0.2)
            except queue.Empty:
                frame = b""
            self._check_pause()
            if frame:
                ws.send(json.dumps({"realtimeInput": {"audio": {
                    "data": base64.b64encode(frame).decode("ascii"), "mimeType": "audio/pcm;rate=16000"}}}))

    def _receive(self, ws) -> None:
        try:
            for raw in ws:
                self._handle(json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw))
        except Exception as e:  # closed by the server (a refused key arrives this way too), or by us
            if not self._stop.is_set():
                self._closed = e
            return
        if not self._stop.is_set() and not self._goaway:
            self._closed = ConnectionError("the connection closed")

    # -- the answers

    def _handle(self, message: dict) -> None:
        if "goAway" in message:
            self._goaway = True
        content = message.get("serverContent") or {}
        heard = content.get("inputTranscription") or {}
        if heard.get("text"):
            self._add(Kind.SOURCE, heard["text"], heard.get("languageCode", ""))
        said = content.get("outputTranscription") or {}
        if said.get("text"):
            self._add(Kind.TRANSLATION, said["text"], said.get("languageCode", ""))
        if content.get("turnComplete"):
            self._finish_line()

    def _add(self, kind: Kind, piece: str, language: str) -> None:
        now = self._clock()
        with self._lock:
            if kind is Kind.SOURCE:
                self._source = _join(self._source, piece)
                self._language = language or self._language
                text = self._source
            else:
                self._translation = _join(self._translation, piece)
                text = self._translation
            self._last_text = now
            if kind is Kind.TRANSLATION or not self._translation:  # the line's latest translated (or heard) words
                quiet = now - self._voice_at
                self._lag = quiet if self._voice_at and quiet >= VOICE_PAUSE else 0.0  # still speaking: unknown
            cut = self._find_cut()
        self.on_event(LiveEvent(kind, _tidy(text), language=language, lane=self.lane))
        if cut:
            self._finish_line(*cut)

    def _find_cut(self) -> tuple[int, int, float] | None:
        """Where a long line ends: when its translation and the words heard are both at a sentence end. Google's
        translation runs ahead of its transcript at times and behind it at others, so a cut at the translation's full
        stop split the words heard mid-word ("…びっくり" | "しました。"), and matching sentence counts drifted for good
        once one sentence became two. Under the lock."""
        t, s = self._translation, self._source
        if len(t) < LONG_LINE or not _SENTENCE_END.search(t):
            return None
        if _SENTENCE_END.search(s):
            return len(t), len(s), self._lag
        if len(t) >= 2 * LONG_LINE:  # the two never pause together: end at the words heard's last sentence end
            ends = [m.end() for m in _SENTENCES.finditer(s)]
            return len(t), ends[-1] if ends else len(s), self._lag
        return None

    def _check_pause(self) -> None:
        with self._lock:
            pending = bool(self._source or self._translation)
            quiet = self._clock() - self._last_text
        if pending and quiet >= self.config.line_pause_s:
            self._finish_line()

    def _finish_line(self, t_cut: int | None = None, s_cut: int | None = None, lag: float | None = None) -> None:
        """Ends the line being built: all of it, or up to the cuts (what follows them begins the next line)."""
        with self._lock:
            t_cut = len(self._translation) if t_cut is None else t_cut
            s_cut = len(self._source) if s_cut is None else s_cut
            source, translation = self._source[:s_cut].strip(), self._translation[:t_cut].strip()
            self._source, self._translation = self._source[s_cut:].lstrip(), self._translation[t_cut:].lstrip()
            lag = self._lag if lag is None else lag
            language, rest = self._language, (self._source, self._translation)
            self._lag = 0.0
        if source or translation:
            self.on_event(LiveEvent(Kind.LINE, _tidy(translation), source=_tidy(source), language=language,
                                    lane=self.lane, seconds=lag))
        for kind, text in zip((Kind.SOURCE, Kind.TRANSLATION), rest, strict=True):
            if text:  # carried past the cut: the next line has begun
                self.on_event(LiveEvent(kind, _tidy(text), language=language, lane=self.lane))

    # -- words for the user

    def _status(self, message: str) -> None:
        self.on_event(LiveEvent(Kind.STATUS, message, lane=self.lane))

    def _scrub(self, text: str) -> str:
        return text.replace(self.key, "***") if self.key else text

    def _explain(self, error: Exception) -> str:
        """The error in plain words (the provider's own goes to the log)."""
        raw = self._scrub(_describe(error)).lower()
        if "api key" in raw or "api_key" in raw or "unauthenticated" in raw or "unregistered caller" in raw:
            return REFUSED_KEY
        if "not found" in raw or "is not supported" in raw or "404" in raw or "permission" in raw:
            return NO_MODEL  # e.g. 1008 "models/... is not found for API version v1beta"
        if "quota" in raw or "429" in raw or "resource_exhausted" in raw or "rate limit" in raw:
            return "Google's quota for this key is used up for now: captions try again shortly."
        if "(1007)" in raw or "invalid json" in raw or "unknown name" in raw or "invalid argument" in raw:
            return NOT_ACCEPTED  # the request itself: the same one would be refused again
        if isinstance(error, (OSError, TimeoutError)) or "timed out" in raw or "getaddrinfo" in raw:
            return "Can't reach Google: check the internet connection. Captions try again shortly."
        return "The connection to Google broke: captions try again shortly."


REFUSED_KEY = "Google refused the key: check the Gemini key in AI & models."
NO_MODEL = "This key can't use Gemini Live Translate (a preview model) yet."
NOT_ACCEPTED = "Google didn't accept Rflow's request: live captions need an update to Rflow."
GIVE_UP = (REFUSED_KEY, NO_MODEL, NOT_ACCEPTED)  # trying again won't help: the captions stop and say why


def _describe(error: Exception) -> str:
    """For the log and the wording: a connection Google closed as its code and reason (websockets' own text repeats
    them, "received ...; then sent ..."), anything else as itself."""
    close = getattr(error, "rcvd", None)
    if close is not None:
        return f"Google closed the connection ({close.code}): {close.reason}"
    return f"{type(error).__name__}: {error}"


def _level(frame: bytes) -> float:
    """A frame's loudness: RMS, 1.0 = full scale."""
    samples = np.frombuffer(frame[: len(frame) // 2 * 2], dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples))) / 32768 if len(samples) else 0.0


def _tidy(text: str) -> str:
    """Google's pieces of Japanese (or Chinese) come with spaces between them ("なんと 。そうなんです"): gone."""
    return _CJK_GAP.sub("", text)


def _join(text: str, piece: str) -> str:
    """The pieces arrive as the model hears them; a piece that repeats the whole line so far replaces it."""
    if text and piece.startswith(text):
        return piece
    return text + piece
