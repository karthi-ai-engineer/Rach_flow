"""meet_answer: answer the question just asked in an online meeting (phase 43, the owner's idea of 2026-10-09).

Press Ctrl+Alt+J, let the question play, press Ctrl+Alt+J again: what the laptop played is turned into text, sent to the
AI model chosen in Rflow, and the answer appears in a small floating box that never takes the keyboard from the meeting.

    config.py      MeetConfig: every setting in one place, in %APPDATA%\\meet_answer\\settings.json
    recorder.py    the meeting audio: the last seconds before the press (memory only) and the recording itself
    transcribe.py  the profile's speech model, loaded once; a WAV file read for tests
    ask.py         the question to the profile's AI model: the prompt, the meeting's last answers, a clear failure
    history.py     the questions and answers kept as text (never the audio)
    overlay.py     AnswerBox, the floating box
    app.py         the tray icon, the shortcut and the stages wired together (MeetApp)

A program of its own (`uv run python -m meet_answer`), on the owner's rule that it never disturbs Rflow: it imports
Rflow's parts (sst.hotkey, sst.live.wasapi, sst.engines, sst.gateway, sst.settings, sst.theme) and changes none of
them, reads the profile's keys and models without writing them, and Rflow never imports it (tests/test_isolation.py).
"""
