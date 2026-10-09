"""The questions and answers, kept as text in history.jsonl (one JSON object a line); the audio is never kept."""
import json
import logging
from datetime import datetime
from pathlib import Path

from meet_answer import config

log = logging.getLogger(__name__)

KEEP = 500  # entries kept: older ones go when the file grows past KEEP_MAX
KEEP_MAX = 600


def history_file() -> Path:
    return config.DATA_DIR / "history.jsonl"


def add(question: str, answer: str, model: str, path: Path | None = None) -> None:
    path = path or history_file()
    entry = {"time": datetime.now().isoformat(timespec="seconds"), "question": question, "answer": answer, "model": model}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > KEEP_MAX:
            path.write_text("\n".join(lines[-KEEP:]) + "\n", encoding="utf-8")
    except OSError as e:  # a full disk must never cost the answer on screen
        log.warning("Couldn't keep the answer in the history: %s", e)


def read(path: Path | None = None) -> list[dict]:
    path = path or history_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries
