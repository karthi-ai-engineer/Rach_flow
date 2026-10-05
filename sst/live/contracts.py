"""The live pipeline's contracts: its settings, and the events every engine produces.

Every engine (Gemini Live Translate now; a streaming recognizer with a clause translator later) turns audio frames into
the same stream of events, so the caption bar, the transcript and the measurements are written once:

    SOURCE      the words heard in the current line so far (they may still change)
    TRANSLATION its translation so far (may still change)
    LINE        the line is finished: its source and translation are final, and the next line starts
    STATUS      connecting, listening, reconnecting, stopped
    ERROR       something went wrong; the text says what, in plain words
"""
import enum
import time
from dataclasses import dataclass, field

RATE = 16_000  # what the live translation models take: 16 kHz mono, 16-bit
FRAME_MS = 100  # the frame Google recommends for latency: 3,200 bytes at 16 kHz


class Kind(enum.Enum):
    SOURCE = "source"
    TRANSLATION = "translation"
    LINE = "line"
    STATUS = "status"
    ERROR = "error"


@dataclass(frozen=True)
class LiveEvent:
    kind: Kind
    text: str = ""  # SOURCE/TRANSLATION: the whole line so far; LINE: the translation; STATUS/ERROR: the message
    source: str = ""  # LINE: the original words of the finished line
    language: str = ""  # the language the engine says the text is in (BCP-47), if it says
    lane: str = "system"  # "system": what the laptop plays; "mic": the user's own speech (part 2)
    seconds: float = 0.0  # LINE: from the line's first words to its finished translation (the delay a reader sees)
    at: float = field(default_factory=time.monotonic)


@dataclass
class LiveConfig:
    target: str = "en"  # the language captions are translated into (BCP-47)
    echo_target: bool = False  # speech already in the target language: shown as heard, not repeated by the model
    model: str = "gemini-3.5-live-translate-preview"
    line_pause_s: float = 1.5  # a line with no new words for this long is finished (the model may not say so)
    reconnect_s: float = 540.0  # a Live API connection lasts ~10 min: a new one is opened before that
    caption_lines: int = 2  # finished translation lines kept on screen above the one being spoken
    hide_from_capture: bool = True  # the caption bar isn't in screen shares and recordings


# The languages captions can be translated into: the name shown, and the code the model takes. Gemini 3.5 Live
# Translate knows 70+; these are the ones the owner's laptops and colleagues need first, the rest can follow.
LANGUAGES: dict[str, str] = {
    "English": "en", "Japanese": "ja", "Tamil": "ta", "Hindi": "hi", "Chinese (Simplified)": "zh-CN",
    "Chinese (Traditional)": "zh-TW", "Korean": "ko", "Spanish": "es", "French": "fr", "German": "de", "Italian": "it",
    "Portuguese": "pt", "Dutch": "nl", "Russian": "ru", "Arabic": "ar", "Telugu": "te", "Malayalam": "ml",
    "Kannada": "kn", "Bengali": "bn", "Marathi": "mr", "Thai": "th", "Vietnamese": "vi", "Indonesian": "id",
    "Turkish": "tr", "Polish": "pl",
}


def language_name(code: str) -> str:
    return next((name for name, c in LANGUAGES.items() if c == code), code)
