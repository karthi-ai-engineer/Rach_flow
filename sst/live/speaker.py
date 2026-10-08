"""Speaking live translations aloud, sentence by sentence, as they come.

    TRANSLATION / LINE events of the ways spoken -> whole sentences, in order -> a queue -> the voice (its own thread,
    ~0.2 s a sentence) -> the player (Windows' default output)

A sentence is spoken once the translation goes on past it, when its line ends, or when it ends with a full stop and
nothing has come for SETTLE seconds: so "It costs 3." isn't said before ".5 million" arrives, and the last sentence
before a pause isn't held until the line ends (1.5 s later). An interpreter mustn't fall behind for good: with
sentences waiting it speaks faster (up to MAX_SPEED), and one older than STALE seconds is skipped while newer ones wait
(it stays on screen and in the transcript). Only translations are spoken: speech already in the voice's language is
heard as it is. Like an interpreter, it keeps the other apps lower while it runs, if given a Ducker (sst.live.ducking).

Gemini revises what it has translated: it rewrites earlier words, turns a comma into a full stop, re-sends the whole
line, and starts a new line with the end of the last. So what was said is remembered by what it says (words(): case,
spaces and punctuation aside), in its line and for the session's last RECENT sentences, and a sentence that matches one
of them (alike(), or part of one its line said) is a repeat: never said again. Only new sentences are said, in order.
"""
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sst.live.contracts import Kind, LiveEvent

SETTLE = 0.6  # seconds without a new piece after a full stop: that sentence is complete
STALE = 10.0  # seconds a sentence may wait while newer ones queue behind it
CATCH_UP = 0.15  # faster for each sentence waiting
MAX_SPEED = 1.6
TAIL = 0.4  # seconds after the voice ends that a microphone (the room) may still hear it
RECENT = 20  # the session's last sentences remembered: one coming again (a new line re-sends it) isn't said again
SIMILAR = 0.85  # this alike (difflib's ratio of their words()): the same sentence, a word revised
SHORT = 4  # words: a shorter sentence is the same only word for word ("I think so" isn't "I think not")
SHORT_RECENT = 10.0  # seconds a short sentence of another line counts: "Yes." may well be said again later
_END = re.compile(r"[.!?…。！？]+[\"'”’)\]]*")
_ENDS = re.compile(r"[.!?…。！？]+[\"'”’)\]]*\s*$")
_SHORT = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "no", "prof", "inc", "ltd", "co",
          "approx", "u.s", "u.k", "a.m", "p.m"}  # a full stop after these doesn't end the sentence
_WORDS = re.compile(r"\w+")
_NUMBERS = re.compile(r"\d+")

log = logging.getLogger(__name__)


def whole_sentences(text: str) -> tuple[list[str], int]:
    """The sentences at the start of `text` that the text goes on past, and where the last of them ends."""
    spans = _spans(text)
    return [sentence for sentence, _ in spans], spans[-1][1] if spans else 0


def _spans(text: str) -> list[tuple[str, int]]:
    """whole_sentences(), each with where it ends."""
    found, start = [], 0
    for m in _END.finditer(text):
        end = m.end()
        if end >= len(text) or not text[end].isspace():
            continue  # the text doesn't go on past it yet ("3." of "3.5"), or a dot inside a word ("e.g")
        words = text[start:m.start()].split()
        if m.group().startswith(".") and words and words[-1].lower().rstrip(".") in _SHORT:
            continue
        if sentence := text[start:end].strip():
            found.append((sentence, end))
        start = end
    return found


def _all(text: str) -> list[str]:
    """Every sentence of `text`, the unfinished last one too."""
    sentences, end = whole_sentences(text)
    return sentences + ([text[end:].strip()] if text[end:].strip() else [])


def words(sentence: str) -> str:
    """What a sentence says: its words, lower case, without punctuation."""
    return " ".join(_WORDS.findall(sentence.lower()))


def alike(a: str, b: str) -> bool:
    """Two sentences (as words()) that say the same: word for word when short, else nearly (a word revised), and never
    with other numbers ("it costs 3 million" isn't "it costs 5 million")."""
    if a == b:
        return True
    if min(a.count(" "), b.count(" ")) + 1 < SHORT or _NUMBERS.findall(a) != _NUMBERS.findall(b):
        return False
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return matcher.real_quick_ratio() >= SIMILAR and matcher.quick_ratio() >= SIMILAR and matcher.ratio() >= SIMILAR


@dataclass
class _Line:
    text: str = ""
    spoken: int = 0  # how much of `text` is said or queued
    at: float = 0.0  # when its last piece came
    said: list[str] = field(default_factory=list)  # its sentences said, queued or let go while muted, as words()


class Speaker:
    """hear(event) from any thread; it speaks on its own thread. `speaking`: the voice can be heard now, or was a moment
    ago (the microphone's way is muted then, where it could hear it). `voice_loader()` gives an object with `rate` and
    synthesize(text, speed); `player` has play(samples, rate), interrupt(), close() and `private`."""

    def __init__(self, voice_loader: Callable, player, lanes: Iterable[str], language: str = "en", speed: float = 1.0,
                 on_problem: Callable[[str], None] = lambda message: None, clock: Callable[[], float] = time.monotonic,
                 ducker=None):
        self._load, self.player, self.language, self.speed = voice_loader, player, language, speed
        self.lanes = set(lanes)
        self._problem, self._clock = on_problem, clock
        self.ducker = ducker  # keeps the other apps lower while it runs (sst.live.ducking), if given
        self._lines: dict[str, _Line] = {}
        self._queue: deque[tuple[float, str, str]] = deque()  # (when it was complete, way, sentence)
        self._recent: deque[tuple[str, float]] = deque(maxlen=RECENT)  # the session's last sentences: (words, when)
        self._lock = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._audible_until = 0.0
        self._last_problem = ""
        self.muted = False
        self.said = self.skipped = self.repeats = 0  # repeats: sentences that came again and weren't said again

    @property
    def speaking(self) -> bool:
        return self._clock() < self._audible_until + TAIL

    @property
    def private(self) -> bool:
        """The voice plays through headphones or a headset: no microphone hears it."""
        return bool(self.player.private)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-voice", daemon=True)
        self._thread.start()
        if self.ducker is not None:
            self.ducker.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._queue.clear()
            self._lock.notify()
        self.player.interrupt()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(3)
        if self.ducker is not None:
            self.ducker.stop()  # the other apps back to their own volume

    def set_lanes(self, lanes: Iterable[str]) -> None:
        with self._lock:
            self.lanes = set(lanes)
            self._queue = deque(item for item in self._queue if item[1] in self.lanes)
            self._lines = {lane: line for lane, line in self._lines.items() if lane in self.lanes}

    def set_speed(self, speed: float) -> None:
        self.speed = speed

    def set_muted(self, muted: bool) -> None:
        """Muted: nothing more is said (what's queued is dropped, what's playing stops) until unmuted."""
        with self._lock:
            self.muted = muted
            if muted:
                self._queue.clear()
                for lane, line in self._lines.items():
                    self._say(lane, line, _all(line.text[line.spoken:]))  # let go: not said after unmuting either
                    line.spoken = len(line.text)
        if muted:
            self.player.interrupt()

    def hear(self, event: LiveEvent) -> None:
        if event.kind not in (Kind.TRANSLATION, Kind.LINE):
            return
        with self._lock:
            if event.lane not in self.lanes:
                return
            line = self._lines.setdefault(event.lane, _Line())
            text = event.text.lstrip()  # a line's first piece may start with a space; its LINE never does
            start = line.spoken
            if not text.startswith(line.text[:start]):  # the model revised the line: from the first sentence it changed
                same = len(os.path.commonprefix([text, line.text[:start]]))
                start = max((end for _, end in _spans(text) if end <= same), default=0)
            line.text, line.at = text, self._clock()
            if event.kind is Kind.LINE:
                self._say(event.lane, line, _all(text[start:]), together=True)
                del self._lines[event.lane]
            else:
                spans = _spans(text[start:])
                self._say(event.lane, line, [sentence for sentence, _ in spans])
                line.spoken = start + (spans[-1][1] if spans else 0)
            self._lock.notify()

    def _say(self, lane: str, line: _Line, sentences: list[str], together: bool = False) -> None:
        """Queue the sentences not said before (all of them as one, `together`) and remember them. Under the lock."""
        new = []
        for sentence in sentences:
            said = words(sentence)
            if not said:
                continue  # punctuation alone
            if self._repeats(said, line):
                self.repeats += not self.muted
                continue
            line.said.append(said)
            self._recent.append((said, self._clock()))
            if not self.muted:
                new.append(sentence)
        for sentence in [" ".join(new)] if together and new else new:
            self._queue.append((self._clock(), lane, sentence))

    def _repeats(self, said: str, line: _Line) -> bool:
        """Whether a sentence (as words()) was said already: in its line (alike one, two or three of its sentences in a
        row, or a part of one: a comma made a full stop), or of late in the session."""
        if any(f" {said} " in f" {other} " for other in line.said):
            return True
        for i in range(len(line.said)):
            if any(alike(said, " ".join(line.said[i:i + n])) for n in (1, 2, 3) if i + n <= len(line.said)):
                return True
        now, short = self._clock(), said.count(" ") + 1 < SHORT
        return any(alike(said, other) for other, at in self._recent if not short or now - at <= SHORT_RECENT)

    def _settle(self) -> None:
        """A line whose text ends with a full stop and has had nothing new for SETTLE seconds: that sentence is said."""
        now = self._clock()
        for lane, line in self._lines.items():
            rest = line.text[line.spoken:]
            if rest.strip() and _ENDS.search(rest) and now - line.at >= SETTLE:
                self._say(lane, line, _all(rest), together=True)
                line.spoken = len(line.text)

    def _next(self) -> tuple[str, int] | None:
        """The next sentence to say and how many wait behind it, or None (nothing for now, or stopping)."""
        with self._lock:
            while not self._stop.is_set():
                self._settle()
                while self._queue:
                    at, _, sentence = self._queue.popleft()
                    if self._queue and self._clock() - at > STALE:
                        self.skipped += 1  # newer ones wait: this one stays on screen and in the transcript
                        continue
                    return sentence, len(self._queue)
                self._lock.wait(0.1)
        return None

    def _run(self) -> None:
        try:
            voice = self._load()
        except Exception as e:
            log.exception("The voice couldn't be loaded")
            self._report(f"The voice couldn't start: {e}")
            return
        log.info("The voice is ready (%s, %d Hz): it speaks the ways %s", self.language, voice.rate, sorted(self.lanes))
        try:
            while (item := self._next()) is not None:
                sentence, waiting = item
                speed = min(self.speed * (1 + CATCH_UP * waiting), max(MAX_SPEED, self.speed))
                try:
                    samples = voice.synthesize(sentence, speed)
                    if self.muted or self._stop.is_set():
                        continue
                    self._audible_until = self._clock() + len(samples) / voice.rate + 0.25  # + Windows' buffer
                    left = self.player.play(samples, voice.rate)
                    self._audible_until = self._clock() + left
                    self.said += 1
                    self._last_problem = ""
                except Exception as e:  # one sentence lost; the next is tried
                    log.warning("The voice couldn't say a sentence: %s", e)
                    self._report(f"The voice couldn't play: {e}")
        finally:
            self.player.close()

    def _report(self, message: str) -> None:
        if message != self._last_problem:  # once, not for every sentence
            self._last_problem = message
            self._problem(message)
