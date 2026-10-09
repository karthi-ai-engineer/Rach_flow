"""meet_answer's tests: Qt off-screen, and its data folder in a temporary one (never the real %APPDATA%)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meet_answer import config  # noqa: E402
from sst import downloads  # noqa: E402
from sst.engines import parakeet  # noqa: E402


@pytest.fixture(autouse=True)
def models(monkeypatch, tmp_path_factory):
    """As in Rflow's tests: nothing downloaded, and Parakeet counts as next to the program (no test loads it), so the
    results don't depend on what this laptop has."""
    monkeypatch.setattr(downloads, "DOWNLOADS_DIR", tmp_path_factory.mktemp("downloads"))
    folder = tmp_path_factory.mktemp("parakeet")
    (folder / "encoder.int8.onnx").write_bytes(b"")
    monkeypatch.setattr(parakeet, "MODEL_DIR", folder)


@pytest.fixture
def no_parakeet(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(parakeet, "MODEL_DIR", tmp_path_factory.mktemp("no-parakeet"))


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    folder = tmp_path / "meet_answer"
    monkeypatch.setattr(config, "DATA_DIR", folder)
    return folder
