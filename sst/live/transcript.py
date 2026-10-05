"""The bilingual transcript of a live captions session: one text file per session in the profile's folder, written
line by line as lines finish, so a crash loses at most the line being spoken."""
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sst.live.contracts import MIC, Kind, LiveEvent, language_name

log = logging.getLogger(__name__)


class Transcript:
    """`mine_target`: the language the user's own speech goes into, when that way is on (its lines say "You:")."""

    def __init__(self, folder: Path, target: str, now: Callable[[], datetime] = datetime.now, mine_target: str = ""):
        self.folder, self.target, self._now, self.mine_target = folder, target, now, mine_target
        self.path: Path | None = None  # made with the first finished line: an empty session leaves no file
        self.lines = 0

    def add(self, event: LiveEvent) -> None:
        if event.kind is not Kind.LINE:
            return
        try:
            if self.path is None:
                self.folder.mkdir(parents=True, exist_ok=True)
                started = self._now()
                self.path = self.folder / f"{started:%Y-%m-%d %H-%M-%S} live captions.txt"
                mine = f"; your own speech into {language_name(self.mine_target)}" if self.mine_target else ""
                self.path.write_text(f"Rflow live captions, {started:%Y-%m-%d %H:%M}, translated into "
                                     f"{language_name(self.target)}{mine}\n\n", encoding="utf-8")
            who = "You: " if event.lane == MIC else ""
            entry = f"[{self._now():%H:%M:%S}] {who}{event.source or '(not heard)'}\n"
            if event.text and event.text != event.source:
                entry += f"           {event.text}\n"
            with self.path.open("a", encoding="utf-8") as out:
                out.write(entry + "\n")
            self.lines += 1
        except OSError as e:  # never stop the captions over a file
            log.warning("Couldn't write the live captions transcript: %s", e)
