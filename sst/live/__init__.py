"""Live captions: what the laptop plays (a meeting, a video), shown and translated while people speak.

A pipeline of its own, apart from dictation (the owner's rule): nothing here imports sst.pipeline or sst.dictate, and
tests/test_live_isolation.py fails if anything does. Dictation records a key press and types finished text; live
captions run for as long as the user wants and show text that is still being spoken, so they share no stage.

    WASAPI loopback (wasapi.py)  ->  100 ms frames, 16 kHz mono PCM16
    -> engine (gemini.py: Gemini 3.5 Live Translate)  ->  LiveEvents (contracts.py)
    -> LiveSession (session.py): the transcript (transcript.py) and the caption bar (captions.py, Qt)

Why not dictation's 4-20 s parts: live systems stream short frames to a streaming model and translate whole clauses
(see the research report, "Live speech translation"); English/Japanese word order puts a 2-5 s floor under any live
translation, so the rest of the pipeline must add as little as possible.
"""
