# Contributing to Rflow

Thanks for helping. Rflow is dictation for Windows that keeps your voice on your laptop, so two things matter more than
anything else here: **it must never lose what someone said**, and **it must never send or keep more than the user chose**.

## Reporting a bug or asking for a feature

- **Bugs:** [open an issue](https://github.com/karthi-ai-engineer/rflow-ai/issues/new/choose) with the Rflow version
  (Settings, at the bottom), your Windows version and what you did.
- **Logs help, but read them first.** Rflow's log (Settings → Advanced → Logs) can contain text you dictated. Remove
  anything private before you attach it. Never attach recordings of other people.
- **Features:** open a feature request and say what you want to do, not only how. For a big change, wait for a reply
  before writing code, so your work isn't wasted.
- **Security flaws:** never in a public issue. See [SECURITY.md](SECURITY.md).

## Setting up

You need Windows 10 or 11 (x64; on an ARM laptop, use x64 Python: Whisper's CTranslate2 has no ARM64 build) and
[uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/karthi-ai-engineer/rflow-ai.git
cd rflow-ai
uv sync                                          # the app and the dev tools (pytest, ruff)
uv run python scripts/download_model.py parakeet # the speech model (~630 MB) into models/
uv run sst app                                   # the tray app; quit an installed Rflow first
```

`README.md` explains the layout and the other commands; `CLAUDE.md` holds the project's working rules in detail.

## Before you open a pull request

```
uv run pytest           # every test must pass
uv run ruff check .     # lint
```

- **Tests never touch the real computer.** They must not press real keys, take the focus, use the real clipboard, the
  microphone or the network. Use fakes, as `tests/test_dictate.py` does. A test that types into whatever window is in
  front can type into someone's work.
- **Accuracy changes are measured.** A change meant to make recognition or cleanup more accurate is kept only if
  `sst eval` shows it better on the reading test's held-out sets (C to E). See [docs/accuracy.md](docs/accuracy.md).
  If an engine's output can change (model, decoding, hotwords), change its `signature`: transcriptions are cached by it.
- **Fail closed.** When unsure, keep the user's words. Never type a text with a hole in it, and keep the recording when
  something fails.
- **Privacy:** never log dictated text, keys or server addresses; never upload audio unless the user chose a cloud
  model; never commit `models/`, `recordings/`, keys or anyone's voice.
- **Style:** match the code around yours: compact, comments that explain *why*, line length 130, the ruff rules in
  `pyproject.toml`. Windows APIs are called through `ctypes`. Add a dependency only when it clearly pays for itself.
- **UI changes:** render the window off-screen in both themes (for example with `scripts/make_site_screenshots.py`)
  and add before/after pictures to the pull request.

## Pull requests

1. Fork the repository and branch from an up-to-date `main` (`fix/<name>` or `feature/<name>`).
2. Commit in small steps. The subject is imperative and at most about 72 characters; the body says why.
3. Open the pull request into `main`, fill in the template, and link the issue (`Closes #123`). CI must pass on x64 and
   Windows on ARM.
4. The maintainer reviews it and merges it, or says what's missing.

## Licence

Rflow is released under the [MIT License](LICENSE). By sending a contribution you agree that it is released under the
same licence. The Rflow name and logo are not part of it.
