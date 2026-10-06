"""One live captions session: one or two ways (lanes), each a capture feeding its own engine, and their events going to
one transcript, the log and whoever shows them (the caption bar). Qt-free: the app moves the events to its own thread.

    SYSTEM  what the laptop plays (a meeting, a video)       -> target
    MIC     what the microphone hears (the room, or the user)  -> mic_target (Japanese)

The microphone's lines that come back untranslated (speech already in mic_target: a meeting's Japanese from the
speakers when there are no headphones, or someone speaking Japanese) are dropped, never shown or saved: with Both they
are echo, and with the microphone alone they need no translation.

With speaking on, the translation also goes to a Speaker (sst.live.speaker), and a way that could hear the voice gets
silence while it speaks: the microphone near speakers (not headphones), and what the laptop plays where Windows can't
leave Rflow's own sound out (before Windows 11). So the spoken translation is never heard and translated again.
"""
import logging
import statistics
from collections.abc import Callable

from sst.live.contracts import MIC, Kind, LiveConfig, LiveEvent
from sst.live.transcript import Transcript

log = logging.getLogger(__name__)


class LiveSession:
    """add(lane, capture, engine_factory) starts a way: `capture` has start(on_frame) and stop(); `engine_factory(on_event)`
    makes an engine with start(), feed(frame) and stop() (GeminiLiveTranslate, or a fake in the tests)."""

    def __init__(self, config: LiveConfig, transcript: Transcript | None = None,
                 on_event: Callable[[LiveEvent], None] = lambda event: None):
        self.config, self.transcript, self.on_event = config, transcript, on_event
        self.lanes: dict[str, tuple] = {}  # lane -> (capture, engine)
        self.lags: dict[str, list[float]] = {}  # per way, per line that ended in a pause: complete this long after it
        self.lines: dict[str, int] = {}
        self.speaker = None  # speaks the translation aloud (sst.live.speaker), while the user wants it

    @property
    def running(self) -> bool:
        return bool(self.lanes)

    def add(self, lane: str, capture, engine_factory: Callable) -> None:
        """Start a way; if it can't start, nothing of it keeps running and the error is raised."""
        if lane in self.lanes:
            return
        engine = engine_factory(self._event)
        engine.start()
        try:
            capture.start(self._feed(lane, capture, engine))
        except Exception:
            engine.stop()
            raise
        self.lanes[lane] = (capture, engine)
        config = self.config.for_lane(lane)
        log.info("Live captions started (%s): into %s, %s", lane, config.target, config.model)

    def remove(self, lane: str) -> None:
        capture, engine = self.lanes.pop(lane, (None, None))
        if capture is None:
            return
        capture.stop()
        engine.stop()
        lags, lines = self.lags.get(lane, []), self.lines.get(lane, 0)
        if lags:
            log.info("Live captions stopped (%s): %d lines; translation complete %.1f s after the voice paused "
                     "(median of %d), %.1f s at most", lane, lines, statistics.median(lags), len(lags), max(lags))
        else:
            log.info("Live captions stopped (%s): %d lines", lane, lines)

    def stop(self) -> None:
        for lane in list(self.lanes):
            self.remove(lane)
        self.set_speaker(None)

    def set_speaker(self, speaker) -> None:
        """Speak the translation with `speaker` (started here), or stop speaking (None)."""
        old, self.speaker = self.speaker, speaker
        if old is not None:
            old.stop()
            log.info("The voice said %d sentences (%d skipped to keep up)", old.said, old.skipped)
        if speaker is not None:
            speaker.start()

    def _feed(self, lane: str, capture, engine) -> Callable[[bytes], None]:
        """What a way's engine is fed: its frames, or silence while the voice plays where this way hears it."""
        def feed(frame: bytes) -> None:
            speaker = self.speaker
            if speaker is not None and speaker.speaking and (
                    not speaker.private if lane == MIC else getattr(capture, "hears_self", False)):
                frame = bytes(len(frame))
            engine.feed(frame)
        return feed

    def _event(self, event: LiveEvent) -> None:
        if event.kind is Kind.LINE:
            if event.lane == MIC and not event.text:
                log.debug("Live captions: an untranslated line from the microphone dropped (echo): %s", event.source)
                return
            self.lines[event.lane] = self.lines.get(event.lane, 0) + 1
            if event.seconds:
                self.lags.setdefault(event.lane, []).append(event.seconds)
            log.debug("Live line (%s, complete %.1f s after the pause): %s -> %s", event.lane, event.seconds,
                      event.source, event.text)
            if self.transcript is not None:
                self.transcript.add(event)
        if self.speaker is not None:
            self.speaker.hear(event)
        self.on_event(event)
