# Rflow

[![CI](https://github.com/karthi-ai-engineer/Rach_flow/actions/workflows/ci.yml/badge.svg)](https://github.com/karthi-ai-engineer/Rach_flow/actions/workflows/ci.yml)

**Speak anywhere, Rflow types it.** Hold **Ctrl+Win** in any Windows app, speak, let go: your words are typed where
your cursor is. Speech is recognised **on your laptop** by NVIDIA Parakeet (English), so your voice never leaves it;
or choose Whisper (99 languages), a cloud model with your own key, or your own server. Optionally, an AI model you
choose cleans up the text (punctuation, fillers, your own words).

## Install

Download **`Rflow-Setup.exe`** (the latest version:
[releases/latest](https://github.com/karthi-ai-engineer/Rach_flow/releases/latest), about 90 MB) and run it. It
needs no administrator rights and no Python. Windows 10/11, 64-bit. The first time, Rflow asks how to recognise your
speech: download NVIDIA Parakeet once (about 660 MB; offline from then on), or use a cloud model or your own server.

- Windows may say *"Windows protected your PC"*, because the installer isn't code-signed yet. Click **More info → Run anyway**.
- Rflow then opens its window. The first time, a welcome helps you choose how it recognises your speech, choose your
  microphone (with a live level) and try your first dictation. Closing the window keeps Rflow running in the tray (the icon near the clock), so dictation
  keeps working; it also starts when you sign in. Open the window again from the Start menu or by clicking the tray
  icon; **right-click** the icon for the menu (Quit is there).
- **Updates:** when a new version is published, Rflow shows a banner in its window and a notification. **Update now**
  downloads it, checks it against its published SHA-256, installs it and restarts Rflow.
- Settings and history live in `%APPDATA%\sst`, recordings in `%LOCALAPPDATA%\sst\recordings`, reading tests in
  `%LOCALAPPDATA%\sst\bench`, logs in `%LOCALAPPDATA%\sst\logs`. Uninstall from *Settings → Apps*.
- `rflow-cli.exe` next to it is the command-line tool, e.g. `rflow-cli devices`, `rflow-cli file x.wav` or
  `rflow-cli eval` (score your reading tests).

## Use

Click in any text box (Notepad, Chrome, Slack, Teams, VS Code...) and:

| Keys | What happens |
|---|---|
| hold **Ctrl+Win** while speaking | push-to-talk: types the text when you let go |
| tap **Ctrl+Win**, speak, tap again | hands-free: records until the second tap, then types the text |
| **Ctrl+Win+Space**, speak, Ctrl+Win | hands-free too |
| **Esc** while recording | cancels; nothing is typed |

Windows' own Ctrl+Win shortcuts still work: Ctrl+Win+D (new desktop), Ctrl+Win+←/→ and so on simply drop the recording.
Releasing Win doesn't open the Start menu. The key can be changed in Settings (e.g. the Menu key).

While you speak, a small pill near the bottom of the screen shows your voice as a coral waveform with the time; then
"Typing" (or "Cleaning up" in violet while the AI polishes it), and "Typed" when it's done. It never takes the keyboard
focus. A recording stops by itself after 3 minutes and is typed as usual. The text is pasted where your cursor is, and
whatever you had copied is put back on the clipboard afterwards. Dictated text is kept out of Windows clipboard history
(Win+V).

After a dictation, the microphone stays ready for 5 minutes (Windows shows its microphone icon meanwhile). The next
dictation then starts at once and keeps the moment before you pressed the key, so first words aren't cut off; nothing
is recorded or sent until you press the key. It records a moment after you let go too, for the last word. Change this
in Settings ("Microphone stays ready"); a Bluetooth headset's microphone is never kept open.

**The window** (Rflow 2.0's "Obsidian Signal" design: soft depth on graphite, light and dark, one job per colour) has
five sections:

| Section | What it's for |
|---|---|
| **Home** | the voice orb and how to dictate (drawn as keys), a stats strip (this week, words per minute, days in a row, all time), and your dictations by day, searchable with **Ctrl+K**, with Correct and Copy under the pointer. While Parakeet downloads, it shows the progress and says the key isn't ready yet |
| **Words** | *Your words*: names, products and terms that speech recognition listens for and the AI cleanup spells your way (type to find one, Enter to add, a chip's ✕ to remove with Undo); sound-alikes ("post grass" -> PostgreSQL) and corrections you made twice, to learn. **Snippets**, its second tab: say a short phrase, get your own text (**"my email"** types your email address), alone or inside a sentence, typed exactly as written and never sent to the AI |
| **Tools** | **Text Transform** and **Translate**, each with its switch, and a box to try them. Text Transform: hold Ctrl+Win and say **"make it concise"**, "make it professional", "bullet points" or "action items" (your own phrases too), or **double-tap Ctrl** for a menu (1-4, U to undo). It works on the selected text in any app, or else your last dictation, checked so no number, name, date, "not" or "maybe" is lost or invented. Translate: select text and press **Ctrl+C twice**: a window at the pointer shows it translated, with the language at the top; **C** copies it, **Enter** replaces the text. Both use the AI connection |
| **AI & models** | how Rflow hears you (the speech model and the microphone with a live meter) and the AI connection (provider, model, key, a Test), and the **Clean up dictation** switch. One level down: **How Rflow hears you**, the speech models: NVIDIA Parakeet (English, fast; downloaded once when chosen, 663 MB), OpenAI Whisper large-v3 turbo (99 languages, downloaded when chosen, slow without an NVIDIA card), **Scan this PC**, the **cloud** (OpenAI, Groq or Google Gemini with your own key, after a warning that your voice goes to the provider; Parakeet takes over if the provider can't be reached) and **your own server** (Whisper on vLLM, a company AI gateway, any server with OpenAI's transcription API). And **AI connection**: the provider, key, model and backup model (below) |
| **Settings** | the dictation key, sounds, starting with Windows, numbers as numbers, keeping recordings, keeping the microphone ready, updates. **Advanced**: the voice pipeline, troubleshooting steps, Windows' voice effects, the **Reading test** (below), **Profiles** (below) and the logs |

The **voice pipeline** cuts long dictations at your pauses (or at 20 s) and transcribes them while you speak; the parts
are merged, then your dictionary fixes known mishearings, numbers, dates, times and money are written as such ("25%",
"3:30 PM"), the AI cleanup polishes the text, and a guard keeps it from changing numbers, names or meaning.

The first run takes three steps: how Rflow should hear you (Parakeet on this PC, or a cloud model), a first dictation,
and an optional AI connection (paste a key; Rflow picks the provider's fast model). The window follows Windows' light
or dark mode. Changes in Settings apply at once; the AI connection has a Save button.

**AI cleanup (optional):**

1. Choose a **provider** and give it what it needs:

   | Provider | What to enter |
   |---|---|
   | OpenAI, Anthropic, Google Gemini, Groq | an **API key** ("Get a key" opens the provider's page) |
   | Ollama (on this computer) | nothing: it uses `http://localhost:11434/v1` (change it if yours runs elsewhere) |
   | vLLM or another OpenAI-compatible server | its **address** (e.g. `http://localhost:8000/v1`, LM Studio, a company AI gateway) and a key if it needs one |

2. **Load models**, choose a **model** and optionally a **backup model**, click **Test**, then **Save**. Fast chat
   models suit dictation (e.g. gpt-4o-mini, a Haiku model, a Flash-Lite model, llama-3.1-8b-instant); models that
   "think" first are usually too slow.
3. Add **your words** in the Dictionary (names, company, products, tech terms), so they come out spelled right.
   Speech recognition listens for them too, even with AI cleanup off: on the owner's reading test, errors on names and
   terms fell from 40% to 24.5%. Add names and terms, not everyday words, which would be heard where you didn't say them.

Each provider gets the request it understands: Anthropic its own Messages API, OpenAI without the options only
self-hosted models need, and so on. Switching the provider back and forth keeps each one's key and address. Only the
finished text goes to the provider, never audio. If the model fails, the backup is used; if the provider is slow or
unreachable, the text is typed as heard at once and the pill says "Typed as heard". Home keeps both versions (hover a
dictation). Keys are stored in `gateway.json`, encrypted for your Windows account (DPAPI).

**Profiles:** several people on one computer, or a work and a private setup, each get a profile (the button under the
logo, or the *Profiles* page). Each profile has its own dictation key, microphone, words, AI provider and keys,
dictations, stats and reading tests; a new one starts with the welcome. The first profile keeps its files in
`%APPDATA%\sst`, the others in `%APPDATA%\sst\profiles\<name>`.

**Reading test:** the *Reading test* page measures how well Rflow understands *your* voice, microphone and words. There
are 5 sets of 30 short sentences; each test is one set (Record / Stop, or Space; about 6 minutes; you can leave and
continue later), and **New test** moves on to the next set. Sets A and B are for practice: the words Rflow suggests for
Your words come from them. Sets C to E are the real test, so a better score there isn't just learned by heart. **Score**
scores this test; **Score all tests** scores every test together, which gives a much surer answer. Rflow shows:

- the share of words it got wrong with speech recognition alone and with your cleanup model and backup model, with a 95%
  range, and whether a setup is really better than the first one or just lucky
- errors on names and terms apart, the time per sentence, and the words it misheard most
- a warning when a microphone sounds like a phone call (a Bluetooth headset while its microphone is on), clips or is very
  quiet

Tick the suggested words and click **Add to Your words**, then **Score again** to see the difference. Recordings and
results (`report.md`, `results.json`, and `session.json` with the set and microphone) stay in
`%LOCALAPPDATA%\sst\bench\<date>`, and results for all tests go to `bench\summary`. `rflow-cli eval` scores every test
again from the command line: `--model <name>` tries another cleanup model, `--no-cleanup` skips cleanup,
`--words "A,B"` tries other words instead of Your words (`--no-words`: none), and
`--degrade narrowband` (or `gain:-20`) shows what a worse microphone would do to the same recordings. How this feeds the
accuracy work is in [docs/accuracy.md](docs/accuracy.md).

- Using Wispr Flow too? Quit it first: it also listens to Ctrl+Win, and both would type. Rflow warns you if it's running.
- Nothing typed into one particular app? That app is probably running as administrator; Windows doesn't let normal
  programs type into those.

**From the source** (developers): `uv run sst app` starts the app (window and tray; quit the installed Rflow first:
only one can dictate); `dictate.cmd` / `uv run sst dictate` is the same
in a console window, without cleanup. Also `uv run sst web` (a Record button in the browser, served on 127.0.0.1 only),
`uv run sst start` (record in the terminal), `uv run sst file x.wav`, `uv run sst devices` and
`uv run sst eval [<folder>...]` (score reading tests).

## Setup (from source)

Needs Windows 10/11 and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/karthi-ai-engineer/Rach_flow.git
cd Rach_flow
uv sync                                          # install dependencies into .venv
uv run python scripts/download_model.py parakeet # ~630 MB model into models/, and its bpe.vocab for Your words
```

## Development

```
uv run pytest           # tests (they use fakes: no keys pressed, no microphone, model or network needed)
uv run ruff check .     # lint
```

**Build the installer** with **`build_installer.cmd`** (needs the model in `models/` and
[Inno Setup 6](https://jrsoftware.org/isinfo.php): `winget install JRSoftware.InnoSetup`). It builds `Rflow.exe` and
`rflow-cli.exe` with PyInstaller, checks with the model that they really transcribe and open their windows, then takes
the model out again and writes `dist\Rflow-Setup-<version>.exe` (Rflow downloads Parakeet when the user chooses it).

**Release** (this is what users' Update button picks up):

1. Raise `__version__` in `sst/__init__.py` (e.g. `1.1.0`) in the phase's pull request, and merge it.
2. Tag `main` with `v1.1.0` and push the tag.
3. The Release workflow builds the installer and publishes a GitHub Release with `Rflow-Setup.exe` and
   `Rflow-Setup.exe.sha256`. The website's download button and every installed Rflow see it right away.

**Website:** `site/` is a static page for Vercel (Root Directory `site`, no build step). Its download button links to
`releases/latest/download/Rflow-Setup.exe`, so it never needs changing for a new version.

Work happens phase by phase: an issue, a branch and a pull request into `main`, checked by CI.
`CLAUDE.md` has the working rules, and `HANDOFF.md` says where things stand and what comes next.

## Layout

```
Rach_flow/
├─ site/                      the download website (Vercel): index.html, screenshots, icon
├─ dictate.cmd                double-click: dictate in a console window (from source)
├─ web.cmd                    double-click: web page
├─ start.cmd                  double-click: terminal version
├─ build_installer.cmd        double-click: build dist\Rflow-Setup-<version>.exe
├─ CLAUDE.md                  working rules (branches, PRs, authorship)
├─ HANDOFF.md                 current state and next steps, to resume on any device
├─ .github/                   CI, CodeQL, Release, Dependabot, issue and PR templates
├─ docs/accuracy.md           the accuracy plan: research summary, target pipeline, phases
├─ docs/research/             the full accuracy research: report with sources, and the detailed notes
├─ tests/                     pytest suite
├─ scripts/download_model.py  fetches models into models/
├─ scripts/build_installer.py PyInstaller -> model in -> smoke tests -> model out -> Inno Setup
├─ scripts/make_*.py          draw the window's small images, the installer's pictures, the website's screenshots
├─ packaging/                 installer recipe: sst_gui.py / sst_app.py (entry points), sst.spec, installer.iss,
│                             notices, images/ (the setup wizard's pictures)
├─ models/                    downloaded models (git-ignored)
├─ recordings/                your recordings + transcripts (git-ignored)
└─ sst/                       the Python package (the app's internal name)
   ├─ app.py                  the app: tray icon, recording pill, dictation, updates; "open Rflow again" (Qt)
   ├─ window.py               the window: Home, Words, Tools, AI & models, Settings, the first run; light/dark
   ├─ theme.py                the look (Obsidian Signal): colours, Geist fonts, icons, the soft depth Qt paints
   ├─ ui.py                   the window's widgets: buttons with keycaps, toggles, lamps, keycaps, the voice orb
   ├─ bench.py                the reading test's sets of sentences, test sessions, the fair word comparison
   ├─ evaluate.py             `sst eval`: replays the tests through setups; error rates, 95% ranges, microphones
   ├─ updates.py              in-app updates from GitHub Releases (checksum-verified)
   ├─ settings.py             profiles, settings, history, stats and "start with Windows" (%APPDATA%\sst)
   ├─ gateway.py              AI cleanup: the providers and their request formats, backup model, timeouts, keys (DPAPI)
   ├─ cli.py                  the `sst` command
   ├─ dictate.py              Dictation: hotkey events -> record -> transcribe -> clean up -> type
   ├─ hotkey.py               global hotkeys (low-level keyboard hook) and sending keys
   ├─ paste.py                paste text into the focused app, then restore the clipboard
   ├─ web.py                  local server for the web page (127.0.0.1 only)
   ├─ static/                 the Record / Stop page (index.html), the app icon (sst.ico), the window's images (ui/), Geist (fonts/)
   ├─ audio.py                microphone recording, WAV read/write, measuring a recording, splitting long audio
   └─ engines/
      ├─ __init__.py          the speech model catalog (SPEECH_MODELS: where it runs, languages, size) + load_engine()
      └─ parakeet.py          Parakeet via sherpa-onnx (CPU); audio over 3 minutes is split at pauses
```

## Adding another engine

Speech recognition is a building block: any engine works with any AI cleanup model. Create `sst/engines/<name>.py` with
a class that has `name` (its catalog key), `title` (what reports call it), `signature` (what its text depends on;
`sst eval` caches by it), an optional `words` list (Your words) and `transcribe(audio, sample_rate) -> str`. Then add it
to `SPEECH_MODELS` and `load_engine()` in `sst/engines/__init__.py`. It shows up on AI & models (How Rflow hears you), the app
loads it in the background when chosen, and `uv run sst --engine <name> start` or `sst eval --engine <name>` use it.
