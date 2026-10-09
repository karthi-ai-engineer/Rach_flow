# meet_answer

Answer the question just asked in an online meeting. Press **Ctrl+Alt+J** when a question starts, press it again when
it ends: what the laptop played (Teams, Zoom, Meet...) is turned into text with the speech model chosen in Rflow, sent
to the AI model chosen in Rflow, and the answer appears in a small box at the top right. The box never takes the
keyboard from the meeting, and by default it isn't in screen shares.

```
uv run python -m meet_answer                                   # the tray app (Rflow can run beside it)
uv run python -m meet_answer --file q.wav --speech parakeet    # a recorded question: words heard, answer, timings
uv run pytest meet_answer/tests                                # its tests (Rflow's own test run doesn't include them)
```

| Key | What it does |
|---|---|
| Ctrl+Alt+J | start a recording; again: stop it and answer |
| Ctrl+Alt+J twice quickly | answer what was said in the last 15 seconds |
| Esc (while recording) | drop the recording |

- **Pressed late?** The last 15 seconds before the press are kept in memory (never on disk), so the start of the
  question is still there.
- **Follow-ups:** the meeting's last 3 answers go along, so "and what about the cost?" makes sense. After 20 minutes
  without a question, a new meeting starts.
- **Settings:** tray icon → *Open the settings* (`%APPDATA%\meet_answer\settings.json`, used after a restart):
  `shortcut`, `lookback`, `max_seconds`, `style` (`brief`, `detailed`, `talking points`), `follow_ups`, `notes` (about
  you: role, project; used when relevant), `model` and `speech_model` ("" = Rflow's choice), `hide_from_share`,
  `save_history`.
- **Kept:** questions and answers as text in `%APPDATA%\meet_answer\history.jsonl`; the audio never. The log has
  timings, not the text.
- **Tell the people in the meeting** that you record it: in many places everyone must agree.

It is separate from Rflow on purpose: it imports Rflow's parts (`sst.hotkey`, `sst.live.wasapi`, `sst.engines`,
`sst.gateway`, `sst.settings`, `sst.theme`, `sst.live.captions.dragged`) and changes none of them, only reads the
profile's settings and keys, and `tests/test_core.py` fails if `sst` ever imports `meet_answer`.
