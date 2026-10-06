"""The tray app: the pill, what TrayApp does for the window, and "open Rflow again" (built off-screen: nothing appears
on the screen, nothing takes focus)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import dataclasses  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtNetwork import QLocalServer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from sst import app as sst_app  # noqa: E402
from sst import settings as settings_module  # noqa: E402
from sst.engines.cloud import CLOUD, REMOTE  # noqa: E402
from sst.gateway import GatewayConfig  # noqa: E402
from sst.settings import Profile, Profiles, Settings  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("state, message", [("recording", ""), ("transcribing", ""), ("typed", ""), ("typed_raw", ""),
                                            ("typed_local", ""), ("cancelled", ""),
                                            ("ignored", ""), ("warning", "Almost silent: check the microphone."),
                                            ("error", "Could not open the microphone")])
def test_pill_draws_every_state(state, message):
    pill = sst_app.Pill(level=lambda: 0.05)
    pill.show_state(state, message)
    assert pill.isVisible() and not pill.grab().isNull()
    pill.show_state("idle")
    assert not pill.isVisible()


def test_pill_grows_to_fit_long_messages():
    pill = sst_app.Pill()
    pill.show_state("typed")
    short = pill.width()
    pill.show_state("warning", "Almost silent: check the microphone.")
    assert pill.width() > short


def test_only_the_pill_lets_clicks_through_and_nothing_takes_the_focus(monkeypatch):
    import ctypes
    styles = {}
    user32 = SimpleNamespace(GetWindowLongPtrW=lambda hwnd, index: 0x20 | 0x100,  # click-through already, and another bit
                             SetWindowLongPtrW=lambda hwnd, index, style: styles.__setitem__(hwnd, style))
    monkeypatch.setattr(ctypes.windll, "user32", user32)
    sst_app._no_activate(1)  # the pill
    sst_app._no_activate(2, click_through=False)  # Translate's popup, the Text Transform menu
    NOACTIVATE, TRANSPARENT, TOOLWINDOW = 0x08000000, 0x20, 0x80
    assert styles[1] & NOACTIVATE and styles[1] & TRANSPARENT and styles[1] & TOOLWINDOW
    assert styles[2] & NOACTIVATE and not styles[2] & TRANSPARENT and styles[2] & 0x100  # other bits kept


def _fake_tray_app(settings: Settings):
    """TrayApp's methods on a stand-in: apply_settings only records, so nothing touches the real settings file."""
    from sst.pipeline.dictionary import DictionaryStore
    fake = SimpleNamespace(settings=settings, gateway=GatewayConfig(), dictation=None, applied=[], cleanups=0, loads=0,
                           profile=Profile("default"), dictionary=DictionaryStore())

    def apply(new):
        fake.settings = new
        fake.applied.append(new)
    fake.apply_settings = apply
    fake._apply_cleanup = lambda: setattr(fake, "cleanups", fake.cleanups + 1)
    fake._load_speech = lambda: setattr(fake, "loads", fake.loads + 1)
    return fake


def test_adding_words_skips_ones_already_there():
    fake = _fake_tray_app(Settings(vocabulary=["GitHub"]))
    assert sst_app.TrayApp.add_words(fake, ["github", "Tamil", "CodeQL", "Tamil"]) == 2
    assert fake.settings.vocabulary == ["GitHub", "Tamil", "CodeQL"]
    assert sst_app.TrayApp.add_words(fake, ["TAMIL"]) == 0 and len(fake.applied) == 1  # nothing new: nothing saved


def test_removing_a_word():
    fake = _fake_tray_app(Settings(vocabulary=["GitHub", "Tamil"]))
    sst_app.TrayApp.remove_word(fake, "GitHub")
    assert fake.settings.vocabulary == ["Tamil"]


def test_saving_the_cleanup_keeps_the_other_settings(monkeypatch):
    monkeypatch.setattr(GatewayConfig, "save", lambda self, path=None: None)
    fake = _fake_tray_app(Settings(hotkey="menu", vocabulary=["Tamil"]))
    sst_app.TrayApp.save_cleanup(fake, True, "model-a", "model-b", GatewayConfig("http://localhost:11434/v1", ""))
    s = fake.settings
    assert (s.cleanup, s.cleanup_model, s.cleanup_fallback, s.hotkey, s.vocabulary) == (True, "model-a", "model-b", "menu",
                                                                                         ["Tamil"])
    assert fake.gateway.base_url == "http://localhost:11434/v1"


def test_the_first_close_tells_once_that_rflow_keeps_running():
    told = []
    fake = _fake_tray_app(Settings())
    fake._notify = lambda title, message, *_: told.append(title)
    fake.hotkey_label = lambda: "Ctrl+Win"
    sst_app.TrayApp.window_closed(fake)
    sst_app.TrayApp.window_closed(fake)
    assert told == ["Rflow is still running"] and fake.settings.told_about_tray


class FakeListener:
    """Stands in for the keyboard hook: no real keys are watched."""

    def __init__(self, hotkey):
        import queue
        self.hotkey, self.events, self.recording, self.running = hotkey, queue.Queue(), False, False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def capture(self, keys):  # Text Transform's menu takes keys while it is open
        self.captured = keys


class FakeEngine:
    """Named after the model it was loaded as, like the real engines."""

    def __init__(self, name="parakeet", language="", api_key="", model="", fallback=None, url=None):
        self.name, self.title, self.words = name, name.title(), []
        if name.startswith("whisper") or name in REMOTE:
            self.language = language  # like the real one: a model that knows many languages
        if name in REMOTE:  # like sst.engines.cloud.CloudEngine
            self.api_key, self.fallback, self.url = api_key, fallback, url
            self.model = model or (CLOUD[name].models[0] if name in CLOUD else "")

    def set_url(self, url):
        self.url = url

    def transcribe(self, audio, rate):
        return "hello"


@pytest.fixture
def tray_app(monkeypatch, tmp_path):
    """The real TrayApp with everything outside the process faked: settings in memory, no model, no hook."""
    from PySide6.QtTest import QTest

    from sst.settings import Stats
    saved, files = {}, {}  # files: each profile's settings by path, as if on disk
    monkeypatch.setattr(settings_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(sst_app.bench, "BENCH_DIR", tmp_path / "bench")
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: files.get(path) or Settings(
        welcomed=path == tmp_path / "settings.json")))  # the first profile is set up already, a new one isn't

    def save_settings(self, path=None):
        saved["settings"] = files[path] = self
    monkeypatch.setattr(Settings, "save", save_settings)
    monkeypatch.setattr(Stats, "load", classmethod(lambda cls, path=None, history=None: Stats()))
    monkeypatch.setattr(Stats, "save", lambda self, path=None: saved.__setitem__("stats", self))
    monkeypatch.setattr(GatewayConfig, "load", classmethod(lambda cls, path=None: GatewayConfig()))
    monkeypatch.setattr(GatewayConfig, "save", lambda self, path=None: saved.__setitem__("gateway", self))
    monkeypatch.setattr(Profiles, "load", classmethod(lambda cls, path=None: Profiles()))
    monkeypatch.setattr(Profiles, "save", lambda self, path=None: saved.__setitem__("profiles", self))
    history = []
    monkeypatch.setattr(sst_app, "add_to_history", lambda text, heard=None, path=None: history.insert(0, {
        "time": "2026-09-30 10:15:00", "text": text}))
    monkeypatch.setattr(sst_app, "read_history", lambda path=None: history)
    loads = []
    monkeypatch.setattr(sst_app, "load_engine", lambda name, *options, **named: loads.append(name) or FakeEngine(
        name, *options, **named))
    monkeypatch.setattr(sst_app, "HotkeyListener", FakeListener)
    monkeypatch.setattr(sst_app.Recorder, "keep_open", lambda self: None)  # the always-on microphone stays closed
    monkeypatch.setattr(sst_app, "wispr_flow_running", lambda: False)
    monkeypatch.setattr(sst_app, "input_device_names", lambda refresh=True: ["Mic A"])
    monkeypatch.setattr(sst_app, "SERVER_NAME", f"Rflow-test-app-{os.getpid()}")
    app = sst_app.TrayApp(quiet_start=True)
    for _ in range(100):  # the model "loads" on its thread
        if app.dictation:
            break
        QTest.qWait(20)
    yield app, saved, history
    app.window.hide()
    app.quit()


def test_the_tray_app_and_its_window_work_together(tray_app):
    app, saved, history = tray_app
    assert app.dictation and app.listener.running and app.window.ready
    assert "Ready: hold Ctrl+Win" in app.window.status_message
    app.window.open("home")
    app._on_result("hello world", "Hello, world.", 2.0)  # what the dictation reports after typing
    assert history[0]["text"] == "Hello, world." and saved["stats"].words == 2
    assert "Hello, world." in [label.text() for label in app.window.pages["home"].findChildren(QLabel)]
    settings_page = app.window.pages["settings"]
    first_listener = app.listener
    settings_page.hotkey.setCurrentIndex(settings_page.hotkey.findData("menu"))  # applied at once
    assert saved["settings"].hotkey == "menu" and app.listener is not first_listener and not first_listener.running
    assert app.hotkey_label() == "Menu key"
    assert app.add_words(["Tamil"]) == 1 and saved["settings"].vocabulary == ["Tamil"]
    app.window.close()
    assert not app.window.isVisible() and saved["settings"].told_about_tray


def test_the_tray_app_shows_its_window_when_opened_again(tray_app):
    app, _, _ = tray_app
    assert not app.window.isVisible()  # started at sign-in: only the tray icon
    assert sst_app.show_running_window()
    from PySide6.QtTest import QTest
    for _ in range(50):
        if app.window.isVisible():
            break
        QTest.qWait(20)
    assert app.window.isVisible()


def test_opening_rflow_again_reaches_the_running_copy(monkeypatch):
    monkeypatch.setattr(sst_app, "SERVER_NAME", f"Rflow-test-{os.getpid()}")
    assert not sst_app.show_running_window()  # nothing is running: the caller starts normally
    server = QLocalServer()
    assert server.listen(sst_app.SERVER_NAME)
    assert sst_app.show_running_window()
    assert server.waitForNewConnection(2000) or server.hasPendingConnections()
    server.close()


def test_each_profile_has_its_own_setup(tray_app):
    app, saved, _ = tray_app
    first_listener = app.listener
    app.add_words(["Tamil"])
    app.window.open("home")
    app.create_profile("Rahul")  # switches to it
    assert app.profile.name == "Rahul" and app.settings.vocabulary == [] and not app.settings.welcomed
    assert app.window.isVisible() and app.window.current_page() == "welcome"  # a new profile starts with the welcome
    assert "Rahul" in app.window.profile_button.text()
    app.apply_settings(dataclasses.replace(app.settings, hotkey="menu", welcomed=True))
    assert app.listener is not first_listener and app.hotkey_label() == "Menu key"
    app.switch_profile("default")
    assert app.settings.vocabulary == ["Tamil"] and app.hotkey_label() == "Ctrl+Win"  # the first profile's own again
    assert saved["profiles"].active == "default"
    app.delete_profile("rahul")
    assert [p.id for p in app.profiles.items] == ["default"]


def _wait_for(condition, qt_wait):
    for _ in range(100):
        if condition():
            return True
        qt_wait(20)
    return condition()


def test_another_speech_model_loads_in_the_background_and_takes_over(tray_app, whisper_downloaded):
    from PySide6.QtTest import QTest

    app, saved, _ = tray_app
    first = app.dictation.engine
    assert first.name == "parakeet" and app.speech_in_use() == "parakeet"
    app.add_words(["Tamil"])
    app.choose_speech_model("whisper-turbo")
    assert saved["settings"].speech_model == "whisper-turbo"
    assert _wait_for(lambda: app.speech_in_use() == "whisper-turbo", QTest.qWait)
    assert app.dictation.engine is not first and app.dictation.engine.words == ["Tamil"]  # Your words go along
    assert not app.loading_speech and "Ready" in app.window.status_message


def test_a_model_not_downloaded_or_unknown_is_not_chosen(tray_app):
    app, saved, _ = tray_app
    app.choose_speech_model("whisper-turbo")  # not downloaded: the page offers the download instead
    app.choose_speech_model("no-such-model")
    assert app.settings.speech_model == "parakeet" and app.speech_in_use() == "parakeet"


def test_a_failed_switch_keeps_the_model_in_use(tray_app, monkeypatch, whisper_downloaded):
    from PySide6.QtTest import QTest

    app, _, _ = tray_app

    def broken(name, *options):
        raise OSError("not downloaded")
    monkeypatch.setattr(sst_app, "load_engine", broken)
    told = []
    monkeypatch.setattr(app, "_notify", lambda title, message, *_: told.append(message))
    app.choose_speech_model("whisper-turbo")
    assert _wait_for(lambda: not app.loading_speech, QTest.qWait)
    assert app.speech_in_use() == "parakeet" and "still using NVIDIA Parakeet" in told[-1]


def test_switching_profiles_switches_the_speech_model(tray_app, whisper_downloaded):
    from PySide6.QtTest import QTest

    app, _, _ = tray_app
    app.create_profile("Rahul")
    app.choose_speech_model("whisper-turbo")
    assert _wait_for(lambda: app.speech_in_use() == "whisper-turbo", QTest.qWait)
    app.switch_profile("default")  # Karthi's profile still uses Parakeet
    assert _wait_for(lambda: app.speech_in_use() == "parakeet", QTest.qWait)


def test_downloading_a_speech_model_then_using_it(tray_app, monkeypatch, whisper_downloaded):
    from PySide6.QtTest import QTest
    app, saved, _ = tray_app
    seen = []

    def fake_download(model, progress, cancelled):
        progress(model.size // 2, model.size)
        seen.append(app.downloading)
        progress(model.size, model.size)
    monkeypatch.setattr(sst_app.downloads, "download", fake_download)
    app.download_speech_model("whisper-turbo")
    assert _wait_for(lambda: app.speech_in_use() == "whisper-turbo", QTest.qWait)
    assert app.downloading is None and saved["settings"].speech_model == "whisper-turbo"
    app.set_speech_language("ta")
    assert app.dictation.engine.language == "ta"  # no reload needed


def test_a_cancelled_or_failed_download_changes_nothing(tray_app, monkeypatch):
    from PySide6.QtTest import QTest
    app, _, _ = tray_app
    told = []
    monkeypatch.setattr(app, "_notify", lambda title, message, *_: told.append(message))
    for error in (sst_app.downloads.Cancelled("cancelled"), sst_app.downloads.DownloadError("no connection")):
        def fake_download(model, progress, cancelled, error=error):
            raise error
        monkeypatch.setattr(sst_app.downloads, "download", fake_download)
        app.download_speech_model("whisper-turbo")
        assert _wait_for(lambda: app.downloading is None, QTest.qWait)
    assert app.speech_in_use() == "parakeet" and len(told) == 1 and "no connection" in told[0]


def test_only_a_model_not_in_use_can_be_removed(tray_app, monkeypatch, whisper_downloaded):
    removed = []
    monkeypatch.setattr(sst_app.downloads, "remove", lambda model: removed.append(model.folder))
    app, _, _ = tray_app
    app.remove_speech_model("parakeet")  # comes with Rflow: nothing to remove
    app.remove_speech_model("whisper-turbo")
    assert removed == ["faster-whisper-large-v3-turbo"]
    app.choose_speech_model("whisper-turbo")
    app.remove_speech_model("whisper-turbo")  # chosen now: stays
    assert removed == ["faster-whisper-large-v3-turbo"]


def test_scanning_the_computer_times_the_model_in_use(tray_app, monkeypatch, tmp_path):
    import numpy as np
    from PySide6.QtTest import QTest
    app, _, _ = tray_app
    pc = sst_app.scan.Computer(processor="Test CPU", cores=4, threads=8, memory_gb=16, free_memory_gb=8,
                               free_disk_gb=100, score=sst_app.scan.REFERENCE_SCORE)
    monkeypatch.setattr(sst_app.scan, "computer", lambda: pc)
    monkeypatch.setattr(sst_app.scan, "SCAN_FILE", tmp_path / "scan.json")
    monkeypatch.setattr(sst_app, "_sample_sentence", lambda: (np.zeros(16_000, dtype=np.float32), 16_000))
    app.scan_computer()
    assert _wait_for(lambda: not app.scanning, QTest.qWait)
    verdicts = {v["key"]: v for v in app.last_scan["verdicts"]}
    assert verdicts["parakeet"]["measured"] and verdicts["parakeet"]["level"] == "recommended"
    assert not verdicts["whisper-turbo"]["measured"]  # not downloaded: estimated
    assert (tmp_path / "scan.json").exists()


def test_a_cloud_model_needs_its_key_then_takes_over_with_parakeet_behind_it(tray_app):
    from PySide6.QtTest import QTest

    app, saved, _ = tray_app
    parakeet = app.dictation.engine
    app.choose_speech_model("openai")  # no key yet: the page asks for one instead
    assert app.settings.speech_model == "parakeet"
    app.add_words(["Karthi"])
    app.use_cloud_speech("openai", "sk-test", "gpt-4o-transcribe")
    assert saved["gateway"].key_for("openai") == "sk-test"  # encrypted with the others, shared with AI cleanup
    assert saved["settings"].speech_cloud_models == {"openai": "gpt-4o-transcribe"}
    assert _wait_for(lambda: app.speech_in_use() == "openai", QTest.qWait)
    engine = app.dictation.engine
    assert (engine.api_key, engine.model, engine.words) == ("sk-test", "gpt-4o-transcribe", ["Karthi"])
    assert engine.fallback() is parakeet  # already in memory: nothing to load when the provider fails
    assert "speech: Openai" in app.window.status_message
    app.use_cloud_speech("openai", "sk-new", "whisper-1")  # the card's Save: no reload
    assert app.dictation.engine is engine and (engine.api_key, engine.model) == ("sk-new", "whisper-1")
    app.set_speech_language("ta")
    assert engine.language == "ta"
    app.save_cleanup(False, "", "", GatewayConfig())  # the key is gone: back to Parakeet, the one in memory
    assert _wait_for(lambda: app.speech_in_use() == "parakeet", QTest.qWait)
    assert app.dictation.engine is parakeet


def test_starting_with_a_cloud_model_loads_parakeet_only_when_needed(tray_app):
    from PySide6.QtTest import QTest

    app, _, _ = tray_app
    app.use_cloud_speech("groq", "gsk-test", "")
    assert _wait_for(lambda: app.speech_in_use() == "groq", QTest.qWait)
    app._local = None  # as after a start with Groq chosen: Parakeet isn't in memory
    engine = app.dictation.engine
    assert engine.model == "whisper-large-v3-turbo"  # Groq's first model
    first = engine.fallback()
    assert first.name == "parakeet" and engine.fallback() is first  # loaded once


def test_parakeet_typing_for_the_cloud_is_said_once_in_a_while(tray_app, monkeypatch):
    app, _, _ = tray_app
    told = []
    monkeypatch.setattr(app, "_notify", lambda title, message, *_: told.append((title, message)))
    app._on_state("typed_local", "OpenAI: cannot reach api.openai.com")
    app._on_state("typed_local", "OpenAI: cannot reach api.openai.com")
    assert told == [("Cloud speech unavailable", "Parakeet typed it on this computer (OpenAI: cannot reach "
                                                 "api.openai.com).")]
    assert app.pill.state == "typed_local"


def test_a_model_on_your_own_server_switches_its_address_without_a_reload(tray_app):
    from PySide6.QtTest import QTest

    from sst.gateway import SPEECH_SERVER
    app, saved, _ = tray_app
    app.gateway = GatewayConfig("http://gateway.example/v1", "cleanup-key", "vllm")
    app.choose_speech_model("server")  # nothing set up yet
    assert app.settings.speech_model == "parakeet"
    app.use_server_speech("http://gateway.example/v1", "cleanup-key", "whisper-1")
    assert saved["gateway"].speech_server() == ("http://gateway.example/v1", "cleanup-key")
    assert _wait_for(lambda: app.speech_in_use() == "server", QTest.qWait)
    engine = app.dictation.engine
    assert (engine.url, engine.api_key, engine.model) == ("http://gateway.example/v1", "cleanup-key", "whisper-1")
    assert engine.fallback is not None
    app.use_server_speech("http://localhost:8000/v1", "", "openai/whisper-large-v3-turbo")  # Save: no reload
    assert app.dictation.engine is engine and engine.url == "http://localhost:8000/v1" and engine.api_key == ""
    assert engine.model == "openai/whisper-large-v3-turbo"
    assert (app.gateway.base_url, app.gateway.api_key) == ("http://gateway.example/v1", "cleanup-key")  # AI cleanup's
    app.save_cleanup(False, "", "", app.gateway.with_entry(SPEECH_SERVER, "", ""))  # the server is gone: Parakeet
    assert _wait_for(lambda: app.speech_in_use() == "parakeet", QTest.qWait)


def test_a_new_install_starts_without_a_speech_model_then_downloads_parakeet(no_parakeet, tray_app, monkeypatch,
                                                                              tmp_path):
    from PySide6.QtTest import QTest

    from sst.engines import parakeet
    app, _, _ = tray_app
    assert app.dictation is None and not app.loading_speech
    assert "Choose a speech model" in app.window.status_message
    with pytest.raises(RuntimeError, match="isn't downloaded"):
        app._fallback_engine()  # a cloud model has nothing to fall back on yet
    folder = tmp_path / "downloaded"

    def fake_download(model, progress, cancelled):
        progress(model.size, model.size)
        folder.mkdir()
        (folder / "encoder.int8.onnx").write_bytes(b"")
        monkeypatch.setattr(parakeet, "MODEL_DIR", folder)  # as if the download had finished
    monkeypatch.setattr(sst_app.downloads, "download", fake_download)
    app.download_speech_model("parakeet")  # Parakeet was the setting all along: it loads once downloaded
    assert _wait_for(lambda: app.speech_in_use() == "parakeet", QTest.qWait)
    assert app.listener.running and "Ready: hold Ctrl+Win" in app.window.status_message


def test_a_new_install_can_start_with_a_cloud_model_only(no_parakeet, tray_app):
    from PySide6.QtTest import QTest

    app, _, _ = tray_app
    app.use_cloud_speech("groq", "gsk-test", "")
    assert _wait_for(lambda: app.speech_in_use() == "groq", QTest.qWait)
    assert app.listener.running and app._local is None  # nothing on this computer


def test_the_tray_app_dictates_through_the_voice_pipeline(tray_app):
    app, _, _ = tray_app
    pipeline = app.dictation.pipeline
    assert pipeline is not None and pipeline.engine is app.dictation.engine
    assert pipeline.stages.formatter is not None and pipeline.stages.guard is not None and pipeline.stages.llm is None
    assert app.recorder.preroll_seconds == 2.0 and app.recorder.warm_seconds == float("inf")  # always-on, 2 s pre-roll
    app.add_words(["PostgreSQL"])
    app.add_sound_alike("post grass", "PostgreSQL")
    assert pipeline.stages.dictionary.correct("the post grass server").text == "the PostgreSQL server"
    assert "PostgreSQL" in app.dictation.engine.words  # the recogniser listens for the term too
    app.apply_settings(dataclasses.replace(app.settings, format_text=False))
    assert app.dictation.pipeline.stages.formatter is None
    app.apply_settings(dataclasses.replace(app.settings, voice_pipeline=False, always_on_mic=False))
    assert app.dictation.pipeline is None and app.recorder.warm_seconds == sst_app.WARM_SECONDS  # the classic way



def test_translate_starts_with_ctrl_c_c_and_follows_the_settings(tray_app):
    app, _, _ = tray_app
    controller = app.translator
    assert controller.listener is not None and controller.listener.hotkey.text == "ctrl+c+c" and controller.double_copy
    assert not app.translate_ready()  # no AI model yet
    app.apply_settings(dataclasses.replace(app.settings, translate_shortcut=""))
    assert controller.listener is None  # off
    app.apply_settings(dataclasses.replace(app.settings, translate_shortcut="ctrl+alt+l"))
    assert controller.listener.hotkey.text == "ctrl+alt+l" and not controller.double_copy


def test_snippets_are_read_from_the_profile_at_each_dictation(tray_app):
    app, _, _ = tray_app
    assert app.snippets() == []
    app.apply_settings(dataclasses.replace(app.settings, snippets=[{"cue": "my email", "text": "xyz@gmail.com"}]))
    assert [s.text for s in app.snippets()] == ["xyz@gmail.com"]
    assert app.dictation.snippets == app.snippets  # both ways of dictating ask it
    assert app.dictation.pipeline is None or app.dictation.pipeline.stages.snippets == app.snippets


def test_voice_commands_go_to_text_transform(tray_app, monkeypatch):
    app, _, _ = tray_app
    assert app.voice_command("Make it concise.") is None  # no AI model: typed as said, nothing lost
    assert app.voice_command("Undo that.") == "undo"  # the pill says there is nothing to undo
    monkeypatch.setattr(app, "transform_ready", lambda: True)
    assert app.voice_command("Make it concise.") == "concise"
    assert app.voice_command("Make it concise and send it to Priya.") is None
    app.apply_settings(dataclasses.replace(app.settings, command_phrases={"concise": "trim it"}))
    assert app.voice_command("Trim it.") == "concise" and app.voice_command("Make it concise.") is None
    app.apply_settings(dataclasses.replace(app.settings, voice_commands=False))
    assert app.voice_command("Trim it.") is None
    assert app.dictation.command == app.voice_command  # both ways of dictating ask it
    assert app.dictation.pipeline is None or app.dictation.pipeline.stages.command == app.voice_command
    ran = []
    monkeypatch.setattr(app.transforms, "run_command", ran.append)
    app.signals.command.emit("bullets")  # from the dictation's thread, carried out on the app's
    assert ran == ["bullets"]


def test_text_transform_starts_with_its_shortcut_and_uses_the_cleanup_model(tray_app):
    app, saved, history = tray_app
    controller = app.transforms
    assert controller.listener is not None and controller.listener.hotkey.text == "double ctrl"  # the default
    assert not app.transform_ready()  # no AI model yet
    app.apply_settings(dataclasses.replace(app.settings, transform_shortcut=""))
    assert controller.listener is None  # off
    app.apply_settings(dataclasses.replace(app.settings, transform_shortcut="ctrl+alt+t"))
    assert controller.listener.hotkey.text == "ctrl+alt+t"
    app._on_result("hello there", "Hello there.", 1.0)
    assert controller.last_typed[0] == "Hello there. "  # as typed: the shortcut takes it when nothing is selected
    app.remember("Short.", "A longer original.")
    assert history[0]["text"] == "Short."


def test_live_translation_needs_a_gemini_key_and_asks_once_before_the_first_start(tray_app, monkeypatch):
    app, _, _ = tray_app
    assert "add a Gemini key" in app.start_live() and not app.live_running()
    app.gateway = GatewayConfig(provider="gemini", api_key="AIza-test")
    asked = []
    monkeypatch.setattr(sst_app.QMessageBox, "question", lambda *args: asked.append(args) or
                        sst_app.QMessageBox.StandardButton.No)
    assert app.start_live() == "Not started." and len(asked) == 1 and not app.settings.live_told
    started = []

    class FakeSession:  # a LiveSession's ways, without devices or Google
        transcript = speaker = None

        def __init__(self):
            self.lanes = {}

        def add(self, lane, capture, engine_factory):
            self.lanes[lane] = capture
            started.append(True)

        def remove(self, lane):
            self.lanes.pop(lane, None)

        def stop(self):
            self.lanes.clear()
            started.append(False)
    monkeypatch.setattr(app, "_live_session", lambda config, on_event: FakeSession())
    monkeypatch.setattr(app, "_live_lane", lambda lane, config: (lane, None))  # no device is built
    monkeypatch.setattr(sst_app.QMessageBox, "question", lambda *args: asked.append(args) or
                        sst_app.QMessageBox.StandardButton.Yes)
    assert app.start_live() == "" and app.live_running() and app.settings.live_told and app.live_action.isChecked()
    app.stop_live()
    assert not app.live_running() and started == [True, False] and not app.live_action.isChecked()
    assert app.start_live() == "" and len(asked) == 2  # told once: not asked again
    app.stop_live()


def test_live_translation_hears_the_laptop_and_the_microphone_and_saves_in_the_profile(tray_app):
    app, _, _ = tray_app
    app.gateway = GatewayConfig(provider="gemini", api_key="AIza-test")
    from sst.live.contracts import MIC, SYSTEM
    from sst.live.gemini import GeminiLiveTranslate
    from sst.live.wasapi import Capture
    app.apply_settings(dataclasses.replace(app.settings, live_source="both", live_target="en", live_mic_target="ja"))
    config = app.live_config()
    assert config.lanes == (SYSTEM, MIC) and (config.target, config.mic_target) == ("en", "ja")
    session = app._live_session(config, lambda event: None)
    assert session.transcript.folder == app.profile.folder() / "live captions" and session.transcript.config is config
    for lane, what, target in ((SYSTEM, "output device", "en"), (MIC, "microphone", "ja")):
        capture, engine_factory = app._live_lane(lane, config)  # built, not started: no device is opened
        engine = engine_factory(lambda event: None)
        assert isinstance(capture, Capture) and capture.what == what and isinstance(engine, GeminiLiveTranslate)
        assert (engine.key, engine.config.target, engine.lane) == ("AIza-test", target, lane)


def test_the_source_and_screen_sharing_apply_while_it_runs(tray_app, monkeypatch):
    app, _, _ = tray_app
    calls = []
    monkeypatch.setattr(app.live, "set_source", lambda source: calls.append(("source", source)) or "")
    monkeypatch.setattr(app.live, "set_hidden", lambda hidden: calls.append(("hidden", hidden)))
    monkeypatch.setattr(type(app.live), "running", property(lambda self: True))
    assert app.set_live_source("microphone") == "" and app.settings.live_source == "microphone"
    app.set_live_hidden(False)
    assert not app.settings.live_hide_from_share and calls == [("source", "microphone"), ("hidden", False)]


def test_ctrl_alt_l_starts_and_stops_live_translation_from_any_app(tray_app, monkeypatch):
    app, _, _ = tray_app
    listener = app.live_listener
    assert listener is not None and listener.running and listener.hotkey.label == "Ctrl+Alt+L"
    assert app.live_action.text() == "Live translation\tCtrl+Alt+L"
    toggled = []
    monkeypatch.setattr(app, "toggle_live", lambda: toggled.append(True))
    listener.events.put(("press", 0.0))
    listener.events.put(("release", 0.0))
    app._live_key_pump()
    assert toggled == [True]  # once per press
    app.apply_settings(dataclasses.replace(app.settings, translate_shortcut="ctrl+alt+l"))  # Translate's keys now
    assert app.live_listener is None and not listener.running and app.live_shortcut_clash() == "Translate"
    assert app.live_action.text() == "Live translation"
    app.apply_settings(dataclasses.replace(app.settings, translate_shortcut="ctrl+c+c", live_shortcut="win+alt+l"))
    assert app.live_listener.hotkey.label == "Win+Alt+L" and app.live_action.text() == "Live translation\tWin+Alt+L"


def test_where_the_bar_was_left_is_used_at_the_next_start(tray_app, monkeypatch):
    app, _, _ = tray_app
    app._live_moved([40, 50, 600, 220])
    assert app.settings.live_bar == [40, 50, 600, 220]
    started = []
    monkeypatch.setattr(app.live, "start", lambda config, geometry=None: started.append(geometry))
    app.gateway = GatewayConfig(provider="gemini", api_key="AIza-test")
    app.settings = dataclasses.replace(app.settings, live_told=True)
    app.start_live()
    assert started == [[40, 50, 600, 220]]


def test_past_sessions_are_listed_newest_first(tray_app):
    from datetime import datetime
    app, _, _ = tray_app
    folder = app.live_folder()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "2026-10-05 14-03-12 live captions.txt").write_text(
        "Rflow live translation\n\n[14:03:12] a\n           b\n\n[14:03:20] c\n\n", encoding="utf-8")
    (folder / "2026-10-04 09-00-00 live captions.txt").write_text("Rflow live translation\n\n[09:00:00] x\n\n",
                                                                  encoding="utf-8")
    (folder / "notes.txt").write_text("not a session", encoding="utf-8")
    assert [(began, lines) for began, lines, _ in app.live_sessions()] == [
        (datetime(2026, 10, 5, 14, 3, 12), 2), (datetime(2026, 10, 4, 9, 0, 0), 1)]


def test_speaking_needs_the_voice_downloaded_first_then_speaks(tray_app, monkeypatch):
    app, _, _ = tray_app
    from sst.live.contracts import SYSTEM, LiveConfig
    from sst.live.speaker import Speaker
    monkeypatch.setattr(sst_app.Voice, "ready", lambda self, root=None: False)
    assert app._live_speaker(LiveConfig(speak=True)) is None and app.live_voice_state()[0] == "missing"
    started = []
    monkeypatch.setattr(app, "download_live_voice", lambda: started.append(True))
    app.set_live_speak(True)
    assert app.settings.live_speak and started == [True]
    monkeypatch.setattr(sst_app.Voice, "ready", lambda self, root=None: True)
    speaker = app._live_speaker(LiveConfig(target="en", speak=True, speak_speed=1.3))
    assert isinstance(speaker, Speaker) and speaker.lanes == {SYSTEM} and speaker.speed == 1.3
    assert app.live_voice_state() == ("ready", 0, "")


def test_the_voices_download_shows_its_progress_and_speaks_when_done(tray_app, monkeypatch):
    app, _, _ = tray_app
    notes, speak = [], []
    monkeypatch.setattr(app.live, "set_voice_note", notes.append)
    monkeypatch.setattr(app.live, "set_speak", speak.append)
    app.settings = dataclasses.replace(app.settings, live_speak=True)
    app.voice_downloading = 0
    app._on_voice_progress(42)
    assert app.live_voice_state() == ("downloading", 42, "") and notes[-1] == "Downloading the voice: 42%"
    app._on_voice_done("")
    assert app.voice_downloading is None and notes[-1] == "" and speak == [True]
    app._on_voice_done("the model server answered 503")
    assert app.live_voice_state()[2] == "the model server answered 503"


# ---- never lose work (phase 30)

def test_crashes_leave_a_report_and_the_next_start_finds_it(tmp_path, monkeypatch, caplog):
    import sys
    import threading
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # put back after the test
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    enabled = []
    monkeypatch.setattr(sst_app.faulthandler, "enable", enabled.append)  # not the real one: pytest has its own
    assert sst_app.install_crash_handlers(tmp_path) == ""  # a first start: nothing left behind
    assert (tmp_path / "running").exists() and enabled
    sys.excepthook(ValueError, ValueError("a slot failed"), None)
    assert "Unexpected error" in caplog.text and "a slot failed" in caplog.text
    enabled[0].write("Windows fatal exception: access violation\n")  # what faulthandler writes as Rflow dies
    enabled[0].close()
    left = sst_app.install_crash_handlers(tmp_path)  # the next start: "running" is still there
    sst_app._crash_file.close()
    assert left.startswith("crash-") and "access violation" in (tmp_path / left).read_text(encoding="utf-8")
    sst_app.mark_clean_exit(tmp_path)
    assert not (tmp_path / "running").exists()


def test_quitting_or_signing_out_is_not_a_crash(tmp_path, monkeypatch):
    import sys
    import threading
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    files = []
    monkeypatch.setattr(sst_app.faulthandler, "enable", files.append)
    sst_app.install_crash_handlers(tmp_path)
    files[0].write("a first-chance exception a library caught\n")
    files[0].close()
    sst_app.mark_clean_exit(tmp_path)  # Rflow quit normally afterwards
    assert sst_app.install_crash_handlers(tmp_path) == ""
    sst_app._crash_file.close()


def test_a_start_that_fails_says_why_instead_of_vanishing(tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(sst_app.QMessageBox, "critical", lambda *args: shown.append(args[2]))
    (tmp_path / "running").write_text("1", encoding="utf-8")

    def broken():
        raise PermissionError("C:\\Users\\x\\AppData\\Roaming\\sst is not writable")
    assert sst_app.start(broken, tmp_path) is None
    assert shown[0].startswith("Rflow couldn't start: C:\\Users\\x") and str(tmp_path) in shown[0]
    assert not (tmp_path / "running").exists()  # it said why: not a crash


def test_the_crash_left_behind_is_mentioned_once_rflow_is_up(tmp_path):
    told = []

    class App:
        def _notify(self, title, message, icon):
            told.append(message)
    assert isinstance(sst_app.start(App, tmp_path, left="crash-20261006-101500.log"), App)
    assert told == ["Rflow closed unexpectedly last time. A crash report is in its logs folder "
                    "(crash-20261006-101500.log)."]


def test_a_damaged_dictionary_doesnt_stop_rflow_and_the_user_is_told(tray_app, monkeypatch):
    app, _, _ = tray_app
    told = []
    monkeypatch.setattr(app, "_notify", lambda title, message, icon=None: told.append(message))
    path = app.profile.folder() / "dictionary.db"
    app.dictionary.close()
    for side in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if side.exists():
            side.unlink()
    path.write_bytes(b"not a database" * 50)
    app._load_profile()
    assert told and told[0].startswith("Your dictionary file was damaged")
    app.dictionary.add_term("Kubernetes")  # and it works


def test_settings_that_cant_be_saved_are_said_once_and_still_used(tray_app, monkeypatch):
    app, _, _ = tray_app
    told = []
    monkeypatch.setattr(app, "_notify", lambda title, message, icon=None: told.append(message))

    def full_disk(self, path=None):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(Settings, "save", full_disk)
    app.apply_settings(dataclasses.replace(app.settings, sounds=False))
    app.apply_settings(dataclasses.replace(app.settings, sounds=True))
    assert len(told) == 1 and told[0].startswith("Rflow couldn't save your settings") and app.settings.sounds


def test_an_update_never_cuts_a_dictation_or_live_translation(tray_app, monkeypatch):
    app, _, _ = tray_app
    installed, quit_ = [], []
    monkeypatch.setattr(sst_app.updates, "install", installed.append)
    monkeypatch.setattr(sst_app, "release_running_mutex", lambda: None)
    monkeypatch.setattr(app, "quit", lambda: quit_.append(True))
    doing = ["this dictation"]
    monkeypatch.setattr(app, "busy", lambda: doing[0])
    app._on_update_ready("Rflow-Setup.exe")
    assert installed == [] and app._update_timer.isActive() and app._update_waiting == "Rflow-Setup.exe"
    app._install_when_idle()  # still dictating
    assert installed == []
    doing[0] = ""
    app._install_when_idle()  # done: now it installs
    assert [str(p) for p in installed] == ["Rflow-Setup.exe"] and quit_ and not app._update_timer.isActive()


def test_busy_names_what_an_update_waits_for(tray_app, monkeypatch):
    app, _, _ = tray_app
    assert app.busy() == ""
    monkeypatch.setattr(type(app.live), "running", property(lambda self: True))
    assert app.busy() == "live translation stops"


def test_a_history_that_cant_be_written_never_fails_a_dictation(tray_app, monkeypatch):
    app, _, _ = tray_app
    told = []
    monkeypatch.setattr(app, "_notify", lambda title, message, icon=None: told.append(message))

    def unwritable(text, heard=None, path=None):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(sst_app, "add_to_history", unwritable)
    app._on_result("hello", "Hello.", 1.2)
    app._on_result("again", "Again.", 1.0)
    assert len(told) == 1 and told[0].startswith("Rflow couldn't save your dictations to the history")
