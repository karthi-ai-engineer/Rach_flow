"""Live captions with Gemini 3.5 Live Translate, against a fake Live API server: no network, no key, no audio."""
import base64
import json
import queue
import threading
import time

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from sst.live import gemini
from sst.live.contracts import Kind, LiveConfig
from sst.live.gemini import GeminiLiveTranslate

KEY = "AIza-secret-key"


class FakeServer:
    """One Live API connection: records what the client sends, answers what the test scripts."""

    def __init__(self, setup_reply=None, close_reason=None, refuse_setup=None):
        self.sent, self.closed = [], False
        self.incoming: queue.Queue = queue.Queue()
        self.setup_reply = setup_reply or {"setupComplete": {}}
        self.close_reason = close_reason
        self.refuse_setup = refuse_setup  # an exception: Google closes the connection instead of answering the setup

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if self.refuse_setup:
            raise self.refuse_setup
        return json.dumps(self.setup_reply)

    def __iter__(self):
        while True:
            item = self.incoming.get()
            if isinstance(item, Exception):
                raise item
            if item is None or self.closed:
                if self.close_reason:
                    raise ConnectionError(self.close_reason)
                return
            yield json.dumps(item)

    def say(self, **content):
        self.incoming.put({"serverContent": content})

    def close(self):
        self.closed = True
        self.incoming.put(None)

    @property
    def frames(self):
        return [base64.b64decode(m["realtimeInput"]["audio"]["data"]) for m in self.sent if "realtimeInput" in m]


def closed_by_google(code, reason):
    """What websockets raises when Google closes the connection, as in the owner's first run."""
    return ConnectionClosedError(Close(code, reason), Close(code, reason), rcvd_then_sent=True)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def wait_until(condition, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def live():
    made = []

    def make(servers, **config):
        events, clock = [], Clock()
        servers = list(servers)

        def connect(url):
            assert url.endswith(f"?key={KEY}")
            if not servers:
                raise OSError("no more fake servers")
            server = servers.pop(0)
            if isinstance(server, Exception):
                raise server
            return server
        engine = GeminiLiveTranslate(KEY, LiveConfig(**config), events.append, connect=connect, clock=clock)
        made.append(engine)
        return engine, events, clock
    yield make
    for engine in made:
        engine.stop()


def kinds(events, kind):
    return [e for e in events if e.kind is kind]


def test_the_setup_asks_for_the_translation_and_both_transcripts(live):
    server = FakeServer()
    engine, events, _ = live([server], target="ja")
    engine.start()
    assert wait_until(lambda: server.sent)
    setup = server.sent[0]["setup"]
    assert setup["model"] == "models/gemini-3.5-live-translate-preview"
    config = setup["generationConfig"]
    assert config == {"responseModalities": ["AUDIO"],
                      "translationConfig": {"targetLanguageCode": "ja", "echoTargetLanguage": False}}
    # the setup's own fields: inside generationConfig Google closes the connection ("Unknown name")
    assert setup["inputAudioTranscription"] == {} and setup["outputAudioTranscription"] == {}
    assert wait_until(lambda: any(e.text == "Listening" for e in kinds(events, Kind.STATUS)))


def test_frames_stream_as_base64_pcm(live):
    server = FakeServer()
    engine, _, _ = live([server])
    engine.start()
    frame = bytes(range(256)) * 12 + bytes(128)  # 3,200 bytes: 100 ms at 16 kHz
    engine.feed(frame)
    engine.feed(frame)
    assert wait_until(lambda: len(server.frames) == 2) and server.frames[0] == frame
    audio = next(m for m in server.sent if "realtimeInput" in m)["realtimeInput"]["audio"]
    assert audio["mimeType"] == "audio/pcm;rate=16000"


def test_pieces_build_a_line_and_the_turn_finishes_it(live):
    server = FakeServer()
    engine, events, clock = live([server])
    engine.start()
    assert wait_until(lambda: server.sent)
    server.say(inputTranscription={"text": "今日は", "languageCode": "ja"})
    server.say(inputTranscription={"text": "会議です", "languageCode": "ja"})
    assert wait_until(lambda: len(kinds(events, Kind.SOURCE)) == 2)
    clock.now += 2.5  # the translation trails the words
    server.say(outputTranscription={"text": "Today we", "languageCode": "en"})
    server.say(outputTranscription={"text": " have a meeting.", "languageCode": "en"})
    server.say(turnComplete=True)
    assert wait_until(lambda: kinds(events, Kind.LINE))
    assert [e.text for e in kinds(events, Kind.SOURCE)] == ["今日は", "今日は会議です"]
    assert [e.text for e in kinds(events, Kind.TRANSLATION)] == ["Today we", "Today we have a meeting."]
    line = kinds(events, Kind.LINE)[0]
    assert (line.source, line.text, line.language, line.seconds) == ("今日は会議です", "Today we have a meeting.", "ja", 2.5)


def test_a_piece_that_repeats_the_line_replaces_it():
    assert gemini._join("Hello", "Hello world") == "Hello world"
    assert gemini._join("Hello", " world") == "Hello world"
    assert gemini._join("", "今日") == "今日"


def test_a_pause_finishes_the_line_when_the_model_doesnt(live):
    server = FakeServer()
    engine, events, clock = live([server], line_pause_s=1.5)
    engine.start()
    assert wait_until(lambda: server.sent)
    server.say(inputTranscription={"text": "Good morning."})
    server.say(outputTranscription={"text": "おはようございます。"})
    assert wait_until(lambda: kinds(events, Kind.TRANSLATION))
    assert not kinds(events, Kind.LINE)
    clock.now += 1.6  # no new words since
    assert wait_until(lambda: kinds(events, Kind.LINE))
    assert kinds(events, Kind.LINE)[0].text == "おはようございます。"


def test_a_long_translation_ends_its_line_at_a_full_stop(live):
    server = FakeServer()
    engine, events, _ = live([server])
    engine.start()
    assert wait_until(lambda: server.sent)
    long = "We will start with thirty five servers, and the budget should be twenty five thousand dollars for now"
    server.say(outputTranscription={"text": long})
    assert wait_until(lambda: len(kinds(events, Kind.TRANSLATION)) == 1)
    assert not kinds(events, Kind.LINE)  # long, but mid-sentence
    server.say(outputTranscription={"text": " and the rest later."})
    assert wait_until(lambda: kinds(events, Kind.LINE))


def test_speech_already_in_the_target_language_finishes_as_heard(live):
    server = FakeServer()
    engine, events, clock = live([server], target="en")
    engine.start()
    assert wait_until(lambda: server.sent)
    server.say(inputTranscription={"text": "Let's start.", "languageCode": "en"})  # the model stays silent
    assert wait_until(lambda: kinds(events, Kind.SOURCE))
    clock.now += 2
    assert wait_until(lambda: kinds(events, Kind.LINE))
    line = kinds(events, Kind.LINE)[0]
    assert line.source == "Let's start." and line.text == ""


def test_going_away_opens_a_new_connection_without_losing_audio(live):
    first, second = FakeServer(), FakeServer()
    engine, events, _ = live([first, second])
    engine.start()
    assert wait_until(lambda: first.sent)
    first.incoming.put({"goAway": {"timeLeft": "10s"}})
    assert wait_until(lambda: second.sent)  # set up again on the new one
    engine.feed(bytes(3200))
    assert wait_until(lambda: second.frames) and first.closed
    assert len([e for e in kinds(events, Kind.STATUS) if e.text == "Listening"]) == 2


def test_a_connection_is_renewed_before_googles_ten_minutes(live):
    first, second = FakeServer(), FakeServer()
    engine, _, clock = live([first, second], reconnect_s=540)
    engine.start()
    assert wait_until(lambda: first.sent)
    clock.now += 541
    assert wait_until(lambda: second.sent) and first.closed


def test_a_refused_key_says_so_and_stops_trying(live):
    server = FakeServer(close_reason="API key not valid. Please pass a valid API key.")
    engine, events, _ = live([server, FakeServer()])
    engine.start()
    assert wait_until(lambda: server.sent)
    server.incoming.put(None)  # the server closes the connection with its reason
    assert wait_until(lambda: kinds(events, Kind.ERROR))
    assert kinds(events, Kind.ERROR)[0].text == "Google refused the key: check the Gemini key in AI & models."
    assert wait_until(lambda: any(e.text == "Stopped" for e in kinds(events, Kind.STATUS)))


def test_a_request_google_doesnt_accept_stops_at_once_and_is_logged_once(live, caplog):
    reason = 'Invalid JSON payload received. Unknown name "inputAudioTranscription" at \'setup.generation_config\''
    first = FakeServer(refuse_setup=closed_by_google(1007, reason))
    engine, events, _ = live([first, FakeServer()])
    engine.start()
    assert wait_until(lambda: any(e.text == "Stopped" for e in kinds(events, Kind.STATUS)))
    assert [e.text for e in kinds(events, Kind.ERROR)] == [gemini.NOT_ACCEPTED]  # one try, no reconnecting
    assert caplog.text.count("Unknown name") == 1 and "Google closed the connection (1007)" in caplog.text


def test_a_model_the_key_cant_use_isnt_called_a_refused_key(live):
    reason = "models/gemini-3.5-live-translate-preview is not found for API version v1beta"
    engine, events, _ = live([FakeServer(refuse_setup=closed_by_google(1008, reason))])
    engine.start()
    assert wait_until(lambda: kinds(events, Kind.ERROR))
    assert kinds(events, Kind.ERROR)[0].text == gemini.NO_MODEL


def test_a_first_connection_that_stalls_is_tried_again_quietly(live):
    server = FakeServer()
    engine, events, _ = live([TimeoutError("timed out"), server])
    engine.start()
    assert wait_until(lambda: server.sent) and not kinds(events, Kind.ERROR)


def test_a_connection_google_closes_while_listening_reconnects(live, monkeypatch):
    first, second = FakeServer(), FakeServer()
    engine, events, _ = live([first, second])
    monkeypatch.setattr(engine._stop, "wait", lambda seconds: False)
    engine.start()
    assert wait_until(lambda: first.sent)
    first.close_with = closed_by_google(1011, "Internal error encountered.")
    first.incoming.put(first.close_with)
    assert wait_until(lambda: second.sent)
    assert kinds(events, Kind.ERROR)[0].text == "The connection to Google broke: captions try again shortly."


def test_no_internet_says_so_and_tries_again(live, monkeypatch):
    server = FakeServer()
    engine, events, _ = live([OSError("getaddrinfo failed"), server])
    monkeypatch.setattr(engine._stop, "wait", lambda seconds: False)  # no real back-off wait in the test
    engine.start()
    assert wait_until(lambda: server.sent)  # the second try connected
    error = kinds(events, Kind.ERROR)[0]
    assert error.text.startswith("Can't reach Google")


def test_the_key_never_appears_in_messages(live, caplog):
    engine, events, _ = live([ConnectionError(f"bad request for ...?key={KEY}"), FakeServer()])
    engine._stop.wait = lambda seconds: False
    engine.start()
    assert wait_until(lambda: kinds(events, Kind.ERROR))
    assert all(KEY not in e.text for e in events) and KEY not in caplog.text


def test_when_the_network_falls_behind_the_oldest_audio_goes(live):
    engine, _, _ = live([])
    for i in range(gemini.QUEUE_FRAMES + 3):
        engine.feed(bytes([i]) * 3200)
    assert engine.dropped == 3 and engine._frames.qsize() == gemini.QUEUE_FRAMES
    assert engine._frames.get_nowait()[0] == 3  # the newest audio stays: captions stay live


def test_stop_finishes_the_line_and_says_stopped(live):
    server = FakeServer()
    engine, events, _ = live([server])
    engine.start()
    assert wait_until(lambda: server.sent)
    server.say(inputTranscription={"text": "Last words"})
    assert wait_until(lambda: kinds(events, Kind.SOURCE))
    engine.stop()
    assert kinds(events, Kind.LINE)[-1].source == "Last words"
    assert kinds(events, Kind.STATUS)[-1].text == "Stopped" and not threading.current_thread().daemon
