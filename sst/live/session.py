"""One live captions session: the audio capture feeding an engine, and the engine's events going to the transcript,
the log and whoever shows them (the caption bar). Qt-free: the app moves the events to its own thread."""
import logging
import statistics
from collections.abc import Callable

from sst.live.contracts import Kind, LiveConfig, LiveEvent
from sst.live.transcript import Transcript

log = logging.getLogger(__name__)


class LiveSession:
    """`capture` has start(on_frame) and stop(); `engine_factory(on_event)` makes an engine with start(), feed(frame) and
    stop() (GeminiLiveTranslate, or a fake in the tests)."""

    def __init__(self, config: LiveConfig, capture, engine_factory: Callable, transcript: Transcript | None = None,
                 on_event: Callable[[LiveEvent], None] = lambda event: None):
        self.config, self.capture, self.transcript, self.on_event = config, capture, transcript, on_event
        self.engine = engine_factory(self._event)
        self.lags: list[float] = []  # per line that ended in a pause: its translation complete this long after it
        self.lines = 0
        self.running = False

    def start(self) -> None:
        self.engine.start()
        try:
            self.capture.start(self.engine.feed)
        except Exception:
            self.engine.stop()
            raise
        self.running = True
        log.info("Live captions started: into %s, %s", self.config.target, self.config.model)

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.capture.stop()
        self.engine.stop()
        if self.lags:
            log.info("Live captions stopped: %d lines; translation complete %.1f s after the voice paused (median of %d), "
                     "%.1f s at most", self.lines, statistics.median(self.lags), len(self.lags), max(self.lags))
        else:
            log.info("Live captions stopped: %d lines", self.lines)

    def _event(self, event: LiveEvent) -> None:
        if event.kind is Kind.LINE:
            self.lines += 1
            if event.seconds:
                self.lags.append(event.seconds)
            log.debug("Live line (complete %.1f s after the pause): %s -> %s", event.seconds, event.source, event.text)
            if self.transcript is not None:
                self.transcript.add(event)
        self.on_event(event)
