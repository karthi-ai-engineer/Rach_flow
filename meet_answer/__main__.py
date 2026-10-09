"""uv run python -m meet_answer               the tray app: Ctrl+Alt+J in a meeting
   uv run python -m meet_answer --file q.wav  a recorded question: the words heard and the answer, with timings
   uv run python -m meet_answer --file q.wav --no-ai   only the words heard (no AI call)
   uv run python -m meet_answer --file q.wav --speech parakeet   with Parakeet (local, free) instead of Rflow's model
"""
import argparse
import ctypes
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from meet_answer import config as cfg

MUTEX = "MeetAnswer-running"
ERROR_ALREADY_EXISTS = 183


def _logging(console: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()] if console else []
    try:
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(cfg.DATA_DIR / "meet_answer.log", maxBytes=1_000_000, backupCount=2,
                                            encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def already_running() -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    globals()["_mutex"] = kernel32.CreateMutexW(None, False, MUTEX)  # held until the process ends
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


def run_file(path: Path, ask: bool = True, speech: str = "") -> int:
    """The pipeline on a WAV file, without the tray, the shortcut or the box."""
    from meet_answer.ask import Asker, AskError
    from meet_answer.rflow import RflowSetup
    from meet_answer.transcribe import Transcriber, read_wav
    from sst.live.contracts import RATE

    audio = read_wav(path)
    config = cfg.MeetConfig.load()
    setup, transcriber = RflowSetup.load(), Transcriber(speech_model=speech or config.speech_model)
    t0 = time.perf_counter()
    name = transcriber.prepare(setup)
    print(f"Speech model {name} ready in {time.perf_counter() - t0:.1f} s")
    t0 = time.perf_counter()
    question = transcriber.transcribe(audio, setup)
    print(f"Heard ({len(audio) / RATE:.1f} s of audio, {time.perf_counter() - t0:.2f} s): {question or '(nothing)'}")
    if not ask or not question:
        return 0 if question else 1
    try:
        answer = Asker().ask(question, setup, config)
    except AskError as e:
        print(f"No answer: {e}")
        return 1
    print(f"Answer ({answer.model}, {answer.seconds:.2f} s):\n{answer.text}")
    return 0


def run_app() -> int:
    from PySide6.QtWidgets import QApplication

    from meet_answer.app import MeetApp
    from sst import theme

    if already_running():
        print("meet_answer is already running (its tray icon).")
        return 1
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)  # the box closing never ends it: the tray's Quit does
    theme.load_fonts()
    meet = MeetApp(cfg.MeetConfig.load())
    meet.start()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m meet_answer", description="Answer the question just asked in a meeting.")
    parser.add_argument("--file", type=Path, help="answer the question in this WAV file instead of starting the tray app")
    parser.add_argument("--no-ai", action="store_true", help="with --file: only transcribe")
    parser.add_argument("--speech", default="", help="with --file: this speech model (e.g. parakeet) instead of Rflow's")
    args = parser.parse_args(argv)
    _logging(console=bool(args.file))
    if args.file:
        if not args.file.exists():
            parser.error(f"{args.file} doesn't exist")
        return run_file(args.file, ask=not args.no_ai, speech=args.speech)
    return run_app()


if __name__ == "__main__":
    sys.exit(main())
