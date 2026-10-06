"""Setups on AI & models and in the first run, and what each model costs, on the window (off-screen, PreviewApp)."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sst import window as w  # noqa: E402
from sst.gateway import GatewayConfig  # noqa: E402
from sst.settings import Settings  # noqa: E402

SCAN = {"time": "2026-10-06 10:00", "computer": {}, "verdicts": [
    {"key": "parakeet", "level": "recommended", "seconds": 0.9, "measured": False,
     "reason": "about 0.9 s per sentence (estimated)"},
    {"key": "whisper-turbo", "level": "no", "seconds": 9.5, "measured": False,
     "reason": "needs about 3.3 GB of memory; this computer has 4 GB"}]}


@pytest.fixture(scope="module", autouse=True)
def qt():
    return QApplication.instance() or QApplication([])


def _window(**kwargs) -> tuple[w.MainWindow, w.PreviewApp]:
    app = w.PreviewApp(**kwargs)
    return w.MainWindow(app), app


def _recommended() -> dict:
    return {"gateway": GatewayConfig("", "AIza-key-0000-9f3c", "gemini"),
            "settings": Settings(welcomed=True, cleanup=True, cleanup_model="gemini-3.5-flash-lite")}


def _wait_until(condition, seconds: float = 2.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# ---- AI & models

def test_ai_and_models_opens_with_the_setups_and_marks_the_one_in_use():
    window, _ = _window(**_recommended())
    window.show_page("models")
    card = window.pages["models"].setup_card
    assert list(card.tiles) == ["recommended", "fastest", "multilingual", "local", "custom"]
    tiles = card.tiles
    assert tiles["recommended"].state.text() == "In use" and tiles["fastest"].state_row.isHidden()
    assert tiles["recommended"].speech.text() == "Parakeet, on this PC"
    assert tiles["recommended"].ai.text() == "Gemini 3.5 Flash-Lite, on Gemini"
    assert tiles["recommended"].cost.text() == "About $1.05 a month" and tiles["recommended"].needs.text() == \
        "Gemini key: saved"
    assert tiles["fastest"].needs.text() == "Groq key, free to start" and tiles["fastest"].cost.text() == \
        "About $0.70 a month"
    assert tiles["multilingual"].cost.property("tone") == "warn" and not tiles["multilingual"].cost_lamp.isHidden()
    assert tiles["local"].cost.text() == "Free" and tiles["custom"].cost.text() == "Its cost shows as you choose"
    assert card.panel.isHidden() and not card.hint.isHidden()  # a tile first


def test_the_setups_go_two_by_two_on_a_narrow_window():
    window, _ = _window(**_recommended())
    window.show_page("models")
    page = window.pages["models"]
    window.resize(1000, 700)
    window.grab()
    assert page.setup_card.columns == 3
    window.resize(780, 540)
    window.grab()
    assert page.setup_card.columns == 2 and page.widget().width() <= page.viewport().width()


def test_choosing_fastest_asks_for_the_groq_key_then_the_voice_then_uses_both_parts():
    gateway = GatewayConfig("", "AIza-key-0000-9f3c", "gemini").with_key("openai", "sk-openai-0000")
    window, app = _window(gateway=gateway, settings=Settings(welcomed=True, cleanup=True,
                                                             cleanup_model="gemini-3.5-flash-lite"))
    window.show_page("models")
    page = window.pages["models"]
    card = page.setup_card
    card.tiles["fastest"].clicked.emit()
    assert card.tiles["fastest"].chosen and not card.panel.isHidden() and not card.key_area.isHidden()
    assert "Groq" in card.speech_words.text() and "Your voice goes to Groq" in card.speech_words.text()
    assert "speech $0.42, AI $0.28" in card.cost_words.text() and "doesn't train" in card.free_note.text()
    card.use.click()
    assert "Paste your Groq key first" in card.result.text() and not app.calls  # nothing without the key
    card.key.setText("gsk-test-key-1111")
    assert page.unsaved()  # a key typed and not used yet
    questions = []
    page.confirm = lambda question: questions.append(question) or False
    card.use.click()
    assert "sent to Groq" in questions[0] and not app.calls  # said no: not even the key is saved
    page.confirm = lambda question: True
    card.use.click()
    names = [call[0] for call in app.calls]
    assert names[:3] == ["save_key", "use_cloud_speech", "save_cleanup"]  # the key first, alone
    assert ("use_cloud_speech", "groq", "whisper-large-v3-turbo") in app.calls
    assert (app.gateway.chosen, app.settings.cleanup_model, app.settings.speech_model) == ("groq", "openai/gpt-oss-20b",
                                                                                           "groq")
    assert app.gateway.key_for("gemini") == "AIza-key-0000-9f3c" and app.gateway.key_for("openai") == "sk-openai-0000"
    assert card.tiles["fastest"].state.text() == "In use" and card.use.isHidden() and card.key_area.isHidden()
    assert "in use from the next dictation" in card.result.text() and not page.unsaved()


def test_recommended_downloads_parakeet_first_when_it_isnt_here(no_parakeet):
    gateway = GatewayConfig("", "gsk-key", "groq").with_key("gemini", "AIza-key")
    window, app = _window(gateway=gateway, settings=Settings(welcomed=True, speech_model="groq", cleanup=True,
                                                             cleanup_model="openai/gpt-oss-20b"))
    window.show_page("models")
    card = window.pages["models"].setup_card
    assert card.tiles["fastest"].state.text() == "In use"
    card.select("recommended")
    assert card.key_area.isHidden()  # the Gemini key is saved already
    card.use.click()
    assert ("download_speech_model", "parakeet") in app.calls and app.settings.cleanup_model == "gemini-3.5-flash-lite"
    assert "Parakeet is downloading" in card.result.text()
    app.downloading = ("parakeet", 331_000_000, 663_043_117)
    card.refresh()
    assert card.tiles["recommended"].state.text() == "Downloading 49%"  # the setup chosen, its speech on its way


def test_local_checks_this_pc_first_and_says_plainly_what_it_can_run():
    window, app = _window(**_recommended())
    window.show_page("models")
    card = window.pages["models"].setup_card
    card.select("local")
    assert not card.local.isHidden() and card.key_area.isHidden() and "coming soon" in card.ai_words.text()
    assert "Check this PC first" in card.check_words.text() and not card.use.isEnabled()
    card.scan.click()
    assert ("scan_computer",) in app.calls
    app.last_scan = SCAN
    card.local.buttons["whisper-turbo"].click()
    assert "can't run OpenAI Whisper large-v3 turbo" in card.check_words.text() and not card.use.isEnabled()
    card.local.buttons["parakeet"].click()
    assert "This PC runs NVIDIA Parakeet well" in card.check_words.text() and card.use.isEnabled()
    card.use.click()
    assert not app.settings.cleanup and app.settings.speech_model == "parakeet"
    assert app.settings.cleanup_model == "gemini-3.5-flash-lite"  # kept for later
    assert card.tiles["local"].state.text() == "In use"


def test_custom_shows_what_the_choice_costs_and_why_it_is_red():
    settings = Settings(welcomed=True, cleanup=True, cleanup_model="gemini-flash-latest", speech_model="gemini",
                        speech_cloud_models={"gemini": "gemini-3.6-flash"})
    window, _ = _window(gateway=GatewayConfig("", "AIza-key", "gemini"), settings=settings)
    window.show_page("models")
    card = window.pages["models"].setup_card
    assert card.tiles["custom"].state.text() == "In use" and card.tiles["custom"].cost.property("tone") == "err"
    card.select("custom")
    assert card.use.isHidden() and not card.links.isHidden()
    assert "speech $5.67, AI $15" in card.cost_words.text()
    assert "Speech: About $5.67 a month for typical use: more than 10 times the cheapest option." in card.warnings.text()
    card.links.findChildren(w.QPushButton)[1].click()
    assert window.current_page() == "cleanup"


# ---- each model's cost, and a red one asks once

def test_the_ai_connection_shows_each_model_s_cost_and_asks_once_for_an_expensive_one():
    window, app = _window(**_recommended())
    window.show_page("cleanup")
    page = window.pages["cleanup"]
    assert page.cost.words.text() == "About $1.05 a month for typical use: 20 minutes of dictation a day."
    assert page.cost.lamp.isHidden()
    delegate = page.model.itemDelegate()
    page.model.addItems(["gemini-flash-latest", "gemini-9-unknown"])
    assert delegate._words(page.model.model().index(1, 0)) == ("≈ $15/mo", "err")
    assert delegate._words(page.model.model().index(2, 0)) == ("price unknown", "text3")
    page.model.setCurrentText("gemini-flash-latest")
    assert "more than 10 times the cheapest option" in page.cost.words.text() and not page.cost.lamp.isHidden()
    assert page.cost.words.property("tone") == "err"
    questions = []
    page.confirm = lambda question: questions.append(question) or False
    page.bar.save.click()
    assert "gemini-flash-latest is an expensive model" in questions[0] and app.settings.cleanup_model == \
        "gemini-3.5-flash-lite"
    page.confirm = lambda question: questions.append(question) or True
    page.bar.save.click()
    assert app.settings.cleanup_model == "gemini-flash-latest" and len(questions) == 2
    page.fallback.setCurrentText("gemini-3.5-flash-lite")
    page.bar.save.click()
    assert len(questions) == 2 and app.settings.cleanup_fallback == "gemini-3.5-flash-lite"  # asked once only
    page.model.setCurrentText("my-own-model")
    assert page.cost.words.text().startswith("Price unknown") and page.cost.lamp.isHidden()  # never red


def test_a_cloud_speech_model_shows_its_cost_and_an_expensive_one_asks_with_the_voice_question():
    window, app = _window(gateway=GatewayConfig().with_key("gemini", "AIza-key"))
    page = window.pages["speech"]
    page.show_where("gemini")
    gemini = page.models["gemini"]
    assert gemini.cost.words.text() == "About $2.52 a month for typical use: a mid-priced model."
    assert gemini.cost.words.property("tone") == "warn"
    gemini.model_box.setCurrentText("gemini-flash-latest")
    questions = []
    page.confirm = lambda question: questions.append(question) or True
    gemini.choose.click()
    assert len(questions) == 1 and "sent to Google Gemini" in questions[0] and "expensive model" in questions[0]
    assert app.settings.speech_cloud_models["gemini"] == "gemini-flash-latest"
    gemini.model_box.setCurrentText("gemini-3.5-transcribe")
    gemini.choose.click()
    assert len(questions) == 1  # a model that isn't red asks nothing more
