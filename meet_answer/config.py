"""MeetConfig: every setting of meet_answer in one place, kept in its own folder (never in Rflow's files)."""
import dataclasses
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "meet_answer"
STYLES = ("brief", "detailed", "talking points")  # how the answer is written (ask.STYLE_RULES)


@dataclass
class MeetConfig:
    shortcut: str = "ctrl+alt+j"  # starts and stops a recording (sst.hotkey syntax); free in Teams, Zoom, Meet, Windows
    lookback: float = 15.0  # seconds of meeting audio kept in memory before the press (0: only from the press on)
    max_seconds: float = 180.0  # a recording stops by itself after this long (the look-back not counted)
    style: str = "brief"  # a STYLES value
    follow_ups: int = 3  # the meeting's last answers sent along, so "and what about X?" makes sense
    meeting_gap: float = 20 * 60.0  # seconds without a question after which a new meeting starts (no follow-ups)
    notes: str = ""  # about the user (role, project...), given to the AI to use when relevant
    model: str = ""  # an AI model id on the profile's provider; "" = the model chosen for Rflow's AI cleanup
    speech_model: str = ""  # a sst.engines.SPEECH_MODELS key; "" = the one chosen in Rflow (e.g. Parakeet: free, local)
    hide_from_share: bool = True  # the box isn't in screen shares or recordings
    save_history: bool = True  # questions and answers kept as text in history.jsonl (the audio never)
    box: list[int] = field(default_factory=list)  # the box's [x, y, width, height], where the user left it

    @classmethod
    def load(cls, path: Path | None = None) -> "MeetConfig":
        """The saved settings; unknown keys and values of the wrong type are ignored, a damaged file gives the defaults."""
        path = path or DATA_DIR / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            log.warning("Unreadable %s (%s); using the defaults", path, e)
            return cls()
        if not isinstance(data, dict):
            return cls()
        defaults, values = cls(), {}
        for f in fields(cls):
            default, value = getattr(defaults, f.name), data.get(f.name)
            if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            ok = isinstance(value, type(default)) and not (isinstance(value, bool) != isinstance(default, bool))
            if ok and isinstance(value, list):
                ok = all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            values[f.name] = value if ok else default
        config = cls(**values)
        return config.checked()

    def checked(self) -> "MeetConfig":
        """Values out of range brought back into it, so a hand-edited file can't break a meeting."""
        return dataclasses.replace(
            self, lookback=min(max(self.lookback, 0.0), 120.0), max_seconds=min(max(self.max_seconds, 5.0), 600.0),
            style=self.style if self.style in STYLES else "brief", follow_ups=min(max(self.follow_ups, 0), 10),
            meeting_gap=max(self.meeting_gap, 60.0), box=self.box if len(self.box) == 4 else [])

    def save(self, path: Path | None = None) -> None:
        path = path or DATA_DIR / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # never a half-written file
