"""Scan my computer: the verdicts' rules, the reading of the hardware, and the saved result."""
import dataclasses

import pytest

from sst import engines, scan
from sst.scan import Computer


def _local():
    return [m for m in engines.SPEECH_MODELS.values() if m.where == "local" and m.ready]


def _pc(**changes) -> Computer:
    base = Computer(processor="Test CPU", cores=10, threads=12, memory_gb=32, free_memory_gb=16, free_disk_gb=200,
                    graphics=["Intel(R) Iris(R) Xe Graphics"], score=scan.REFERENCE_SCORE)
    return dataclasses.replace(base, **changes)


def _by_key(verdicts):
    return {v.key: v for v in verdicts}


def test_on_the_reference_laptop_parakeet_is_recommended_and_whisper_is_slow():
    verdicts = _by_key(scan.judge(_pc(), _local(), {"parakeet": 0.9}))
    assert verdicts["parakeet"].level == "recommended" and verdicts["parakeet"].measured
    whisper = verdicts["whisper-turbo"]
    assert whisper.level == "slow" and not whisper.measured and abs(whisper.seconds - 10.0) < 0.01
    assert whisper.reason == "about 10.0 s per sentence (estimated)"


def test_a_faster_processor_makes_whisper_usable_and_a_slower_one_makes_parakeet_slow():
    assert _by_key(scan.judge(_pc(score=scan.REFERENCE_SCORE * 3), _local(), {}))["whisper-turbo"].level == "usable"
    slow = _by_key(scan.judge(_pc(score=scan.REFERENCE_SCORE / 4), _local(), {}))
    assert slow["parakeet"].level == "usable" and slow["whisper-turbo"].level == "slow"
    assert not any(v.level == "recommended" for v in slow.values())  # nothing fast enough to recommend


def test_an_nvidia_card_whisper_can_use_makes_it_fast():
    verdicts = _by_key(scan.judge(_pc(nvidia="NVIDIA GeForce RTX 4060", cuda=True), _local(), {"parakeet": 0.9}))
    assert verdicts["whisper-turbo"].level == "fast"
    assert verdicts["parakeet"].level == "recommended"  # English-only Parakeet stays first among fast ones


def test_too_little_memory_or_disk_is_said_plainly():
    verdicts = _by_key(scan.judge(_pc(memory_gb=4), _local(), {}))
    assert verdicts["whisper-turbo"].level == "no" and "3.3 GB of memory" in verdicts["whisper-turbo"].reason
    assert verdicts["parakeet"].level != "no"
    verdicts = _by_key(scan.judge(_pc(free_disk_gb=1.0), _local(), {}))
    assert verdicts["whisper-turbo"].level == "no" and "free disk space" in verdicts["whisper-turbo"].reason


def test_a_downloaded_model_needs_no_disk_space(whisper_downloaded):
    assert _by_key(scan.judge(_pc(free_disk_gb=0.1), _local(), {}))["whisper-turbo"].level != "no"


def test_little_free_memory_asks_to_close_programs():
    verdicts = _by_key(scan.judge(_pc(free_memory_gb=2), _local(), {"parakeet": 0.9}))
    assert "close some programs" in verdicts["whisper-turbo"].reason
    assert "close some programs" not in verdicts["parakeet"].reason


def test_the_summary_says_what_whisper_can_use():
    assert "no NVIDIA card" in _pc().summary()
    assert "CUDA libraries are missing" in _pc(nvidia="NVIDIA RTX A2000").summary()
    assert "Whisper can use it" in _pc(nvidia="NVIDIA RTX A2000", cuda=True).summary()


def test_this_computer_is_read_without_extra_tools():
    pc = scan.computer(benchmark=False)
    assert pc.processor and pc.cores >= 1 and pc.threads >= pc.cores and pc.memory_gb > 0 and pc.free_disk_gb > 0


def test_the_benchmark_measures_something():
    assert scan.score(seconds=0.05) > 0


def test_the_last_scan_is_kept_and_a_damaged_one_is_ignored(tmp_path):
    path = tmp_path / "scan.json"
    data = scan.save(_pc(), scan.judge(_pc(), _local(), {"parakeet": 0.9}), path)
    assert scan.load(path) == data and data["verdicts"][0]["key"] == "parakeet"
    path.write_text('{"computer": {"bogus": 1}}', encoding="utf-8")
    assert scan.load(path) is None and scan.load(tmp_path / "missing.json") is None


@pytest.mark.parametrize("program, native, said", [
    ("win-amd64", "x64", "x64"),  # Intel or AMD
    ("win-amd64", "ARM64", "x64 on ARM64 (emulated)"),  # a Snapdragon laptop: Rflow (x64) through Windows' emulation
    ("win-arm64", "ARM64", "ARM64"),  # a native ARM64 Python (a developer's own)
    ("win-amd64", "", "x64"),  # an older Windows without IsWow64Process2: the program's own kind
])
def test_the_machine_says_how_rflow_runs_on_it(monkeypatch, program, native, said):
    monkeypatch.setattr(scan.sysconfig, "get_platform", lambda: program)
    monkeypatch.setattr(scan, "_native_machine", lambda: native)
    assert scan.machine() == said


def test_this_computers_machine_is_read_and_shown():
    assert scan.machine() in ("x64", "x64 on ARM64 (emulated)", "ARM64")
    assert "(x64 on ARM64 (emulated))" in _pc(machine="x64 on ARM64 (emulated)").summary()
