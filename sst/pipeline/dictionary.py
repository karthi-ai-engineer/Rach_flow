"""The dictionary stage: the user's words ("Your words") turn known speech-recognition mistakes into the spelling the
user wants, deterministically (the plan's §41-49).

    "post grass is down, ask open ai's team about kuberneties" -> "PostgreSQL is down, ask OpenAI's team about Kubernetes"

Three kinds of change, all on whole words, never across sentence punctuation, and in the preferred term's own casing:
    case   the term itself, cased or spaced differently: "github" -> "GitHub", "java script" -> "JavaScript"
    alias  a spelling the user (or an accepted learned correction) tied to the term: "post grass" -> "PostgreSQL"
    fuzzy  a near miss by spelling or by sound: "kuberneties" -> "Kubernetes", trusted only above the term's threshold

A wrong replacement silently changes what the user said, which is worse than a missed one, so every rule leans towards
leaving the text alone: ordinary English words (static/common_words.txt) are never fuzzy-matched and never re-cased,
words of three letters or less are never fuzzy-matched, a word that is itself one of the user's terms is never turned
into another term, and a near miss that two terms explain equally well is left as it is. What was considered but not
trusted is listed in DictionaryResult.rejected, so a missed correction can be explained.

The terms live in SQLite (DictionaryStore), so the app, the learner and the engine share them and a dictionary of
thousands of entries stays cheap to edit; the engine keeps its own index and rebuilds it when the store's version moves.
"""
import enum
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from sst.pipeline.contracts import DictionaryConfig, DictionaryReplacement, DictionaryResult

log = logging.getLogger(__name__)

COMMON_WORDS_FILE = Path(__file__).resolve().parent.parent / "static" / "common_words.txt"
SCHEMA_VERSION = 1
SOURCES = ("user", "vocabulary", "learned")
MAX_FUZZY_TOKENS = 4  # "cube er net ease" -> "Kubernetes" spans four words
MIN_FUZZY_LETTERS = 4  # "never fuzzy-match tokens of 3 letters or less": too many short words look alike
MIN_LENGTH_RATIO = 0.75  # a window and a term whose lengths differ more than this are different words
SOUND_ALIKE = 0.80  # the same sound skeleton scores 0.85-0.90 (see similarity)
AMBIGUITY_MARGIN = 0.02  # two terms this close to the best score: the text doesn't say which, so neither
REPORT_FLOOR = 0.70  # fuzzy candidates below this aren't worth listing in DictionaryResult.rejected

# Short function words: a phrase never starts or ends with one in a fuzzy match ("the kuberneties" is not one word), and
# the learner trims them off the edges of a correction ("the cloud" -> "Claude" teaches "cloud").
STOPWORDS = frozenset(
    "a an the and or but nor of to in on at by for with from into onto as is are was were be been am it its this that "
    "these those i you he she we they me him her us them my your his our their do does did not no so if than then there "
    "here what which who whom whose when where why how all any some each every very just too also can will would should "
    "could may might must shall has have had about over under up down out off".split())


class TermMode(enum.Enum):
    AUTOMATIC = "automatic"  # aliases, casing and close fuzzy matches are replaced
    CAREFUL = "careful"  # a fuzzy match needs a higher score or a context word nearby; so does an alias that is an ordinary word
    HINT_ONLY = "hint_only"  # never replaced: only given to speech engines that accept vocabulary hints


@dataclass
class Term:
    id: int
    preferred: str
    aliases: list[str] = field(default_factory=list)
    mode: TermMode = TermMode.CAREFUL
    enabled: bool = True
    source: str = "user"  # "user", "vocabulary" (mirrors the app's "Your words"), "learned" (an accepted correction)
    context: list[str] = field(default_factory=list)  # words that, when near, support a CAREFUL fuzzy match


# ---------------------------------------------------------------- text: tokens, matching keys, ordinary words, sound

_APOSTROPHES = str.maketrans("\u2019\u2018\u02bc\u00b4`", "'''''")
_CHUNK = re.compile(r"\S+")
_PIECE = re.compile(r"[^\-\u2010-\u2015/]+")  # words inside a chunk: hyphens, dashes and slashes separate them
# A URL, an e-mail address, a path or a code identifier is never touched, and no phrase reaches across one.
_PROTECTED = re.compile(r"://|@|\\|_|www\.|[a-z0-9]\.[a-z]{2,}", re.IGNORECASE)
_JOINERS = frozenset("-\u2010\u2011/")  # with spaces, what may sit between the words of one phrase ("open-ai", "TCP/IP")
_KEEP = frozenset("+#")  # "C++" and "C#" are not "C"
_VOWELS = frozenset("aeiou")
_POSSESSIVE = ("'s", "\u2019s", "\u02bcs", "'S", "\u2019S")


@dataclass(slots=True)
class Token:
    text: str  # as written, without the punctuation at its edges
    start: int  # character span in the text
    end: int
    norm: str  # NFKC, casefolded, one kind of apostrophe: only for comparing, never for output
    key: str  # norm reduced to letters, digits, marks, "+" and "#": "open-ai" and "OpenAI" share a key


def _edge(c: str) -> bool:
    return c not in _KEEP and unicodedata.category(c)[0] in "PS"


@lru_cache(maxsize=65536)
def normalize(word: str) -> str:
    return unicodedata.normalize("NFKC", word).translate(_APOSTROPHES).casefold()


@lru_cache(maxsize=65536)
def _compact(norm: str) -> str:
    # Marks stay: without them two different Tamil words could share a key.
    return "".join(c for c in norm if c.isalnum() or c in _KEEP or unicodedata.category(c)[0] == "M")


def tokenize(text: str, protect: bool = True) -> list[Token]:
    """The words of `text` with their spans. With `protect`, URLs, e-mail addresses, paths and identifiers are left out
    (a term's own spelling, like "Node.js", is tokenized with protect=False)."""
    tokens = []
    for chunk in _CHUNK.finditer(text):
        if protect and _PROTECTED.search(chunk.group()):
            continue
        for piece in _PIECE.finditer(chunk.group()):
            start, end = chunk.start() + piece.start(), chunk.start() + piece.end()
            while start < end and _edge(text[start]):
                start += 1
            while end > start and _edge(text[end - 1]):
                end -= 1
            if start < end:
                norm = normalize(text[start:end])
                if key := _compact(norm):
                    tokens.append(Token(text[start:end], start, end, norm, key))
    return tokens


def compact_key(phrase: str) -> str:
    """The key a phrase is matched by: "Open AI", "open-ai" and "openai" are all "openai"."""
    return "".join(t.key for t in tokenize(phrase, protect=False))


def joinable(gap: str) -> bool:
    """Whether two words separated by `gap` may be one phrase: spaces and hyphens yes; sentence punctuation, commas,
    quotes, line breaks, or a protected chunk (a URL) no."""
    return all(c in _JOINERS or (c.isspace() and c not in "\n\r\u2028\u2029") for c in gap)


@lru_cache(maxsize=1)
def common_words() -> frozenset[str]:
    """Frequent English words, with and without their apostrophes ("don't", "dont"). Empty if the file is missing, which
    turns fuzzy matching off (DictionaryEngine) rather than letting it loose on ordinary words."""
    try:
        words = COMMON_WORDS_FILE.read_text(encoding="utf-8").split()
    except OSError:
        return frozenset()
    return frozenset(w for word in words for w in (normalize(word), _compact(normalize(word))))


_SUFFIXES = (("'s", ""), ("ies", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ed", "e"), ("ing", ""), ("ing", "e"),
             ("ly", ""), ("d", ""))


@lru_cache(maxsize=65536)
def is_common(word: str) -> bool:
    """Whether a normalized word is ordinary English, also in a regular inflection the list doesn't spell out."""
    words = common_words()
    if word in words or _compact(word) in words:
        return True
    return any(word.endswith(suffix) and len(word) - len(suffix) >= 3 and word[:-len(suffix)] + stem in words
               for suffix, stem in _SUFFIXES)


def speech_hints(terms: Iterable[str]) -> list[str]:
    """The terms worth giving a speech model as hints, or protecting through the AI cleanup: names and technical terms,
    not everyday words. Speech models spell everyday words right without help, and a hint list of them only makes them
    heard where they weren't said; an LLM transcriber (Gemini Flash-Lite) even wrote the whole list into the text, at
    the end of dictations (the owner's log, 2026-10-02: "Let's move stand meeting Thursday morning"). A phrase of
    everyday words stays when it is written as a name ("Visual Studio Code")."""
    out: dict[str, str] = {}
    for term in terms:
        words = str(term).split()
        if not words:
            continue
        everyday = all(is_common(normalize(w).strip(".,;:!?\"()")) for w in words)
        named = len(words) > 1 and all(w[:1].isupper() for w in words)
        if not everyday or named:
            out.setdefault(" ".join(words).casefold(), " ".join(words))
    return list(out.values())


@lru_cache(maxsize=65536)
def phonetic(word: str) -> str:
    """A Metaphone-like sound skeleton of an English word or of a phrase written together: consonants by sound, vowels
    dropped except at the start, voiced and unvoiced pairs merged (ASR hears "cooper" for "kuber"). "kubernetes",
    "kuberneties", "coopernetties" and "cubeernetease" are all KPRNTS; "claude" and "cloud" are both KLT."""
    w = "".join(c for c in word.lower() if "a" <= c <= "z")
    if not w:
        return ""
    if w[:2] in ("kn", "gn", "pn", "wr", "ps"):
        w = w[1:]
    elif w[0] == "x":
        w = "s" + w[1:]
    elif w[:2] == "wh":
        w = "w" + w[2:]
    out, i, n = [], 0, len(w)
    while i < n:
        c, prev = w[i], w[i - 1] if i else ""
        nxt, after = w[i + 1] if i + 1 < n else "", w[i + 2] if i + 2 < n else ""
        step = 1
        if c == prev and c != "c":  # a doubled letter sounds once
            i += 1
            continue
        if c in _VOWELS:
            code = "A" if i == 0 else ""
        elif c == "b":
            code = "" if prev == "m" and i == n - 1 else "P"  # "dumb"
        elif c == "c":
            if nxt == "h":
                code, step = "X", 2
            elif nxt == "i" and after == "a" or nxt == "i" and after == "o":
                code = "X"
            elif nxt in ("e", "i", "y"):
                code = "S"
            else:
                code = "K"
        elif c == "d":
            code = "J" if nxt == "g" and after in ("e", "i", "y") else "T"
        elif c == "g":
            if nxt == "h":
                code, step = ("K" if after in _VOWELS else ""), 2  # "ghost" / "night", "though"
            elif nxt == "n" and (i + 2 == n or w[i + 2:] == "ed"):
                code = ""  # "sign", "signed"
            elif nxt in ("e", "i", "y"):
                code = "J"
            else:
                code = "K"
        elif c == "h":
            code = "H" if nxt in _VOWELS else ""
        elif c == "k":
            code = "" if prev == "c" else "K"
        elif c == "p":
            code, step = ("F", 2) if nxt == "h" else ("P", 1)
        elif c == "q":
            code = "K"
        elif c == "s":
            if nxt == "h":
                code, step = "X", 2
            elif nxt == "c" and after == "h":
                code, step = "SK", 3
            elif nxt == "i" and after in ("a", "o"):
                code = "X"
            else:
                code = "S"
        elif c == "t":
            if nxt == "h":
                code, step = "T", 2  # "th" and "t" are merged: recognisers swap them
            elif nxt == "i" and after in ("a", "o"):
                code = "X"
            elif nxt == "c" and after == "h":
                code = ""
            else:
                code = "T"
        elif c == "v":
            code = "F"
        elif c in "wy":
            code = c.upper() if nxt in _VOWELS else ""
        elif c == "x":
            code = "KS"
        elif c == "z":
            code = "S"
        else:  # f j l m n r
            code = c.upper()
        out.append(code)
        i += step
    return re.sub(r"(.)\1+", r"\1", "".join(out))


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """How alike two compact keys are, 0-1. Spelling first: 1 - edit distance / length ("kuberneties" vs "kubernetes":
    0.91). Two keys with the same sound skeleton score at least 0.85-0.90, rising with their spelling, but only if half
    their letters agree: sound alone ("cube er net ease": 0.87) is enough with a context word, or for a CAREFUL term
    never, because many different words share a skeleton ("cabernets" is KPRNTS too)."""
    ortho = 1 - _levenshtein(a, b) / max(len(a), len(b))
    sound = phonetic(a)
    if ortho >= 0.5 and len(sound) >= 3 and sound == phonetic(b):
        return max(ortho, SOUND_ALIKE + 0.1 * ortho)
    return ortho


def _deletions(code: str) -> set[str]:
    """The code and every code one symbol shorter: two codes share one of these exactly when they are at most one
    substitution, insertion or deletion apart (a symmetric-delete index)."""
    return {code} | {code[:i] + code[i + 1:] for i in range(len(code))}


# ---------------------------------------------------------------- the store

_SCHEMA = [
    """
    CREATE TABLE dictionary_terms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        preferred_term TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'careful',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        source TEXT NOT NULL DEFAULT 'user',
        context_json TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE dictionary_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term_id INTEGER NOT NULL REFERENCES dictionary_terms(id) ON DELETE CASCADE,
        alias TEXT NOT NULL,
        normalized_alias TEXT NOT NULL
    );
    CREATE INDEX dictionary_aliases_term ON dictionary_aliases(term_id);
    CREATE UNIQUE INDEX dictionary_aliases_key ON dictionary_aliases(normalized_alias);
    CREATE TABLE learned_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_phrase TEXT NOT NULL,
        corrected_phrase TEXT NOT NULL,
        seen_count INTEGER NOT NULL DEFAULT 0,
        confirmed_count INTEGER NOT NULL DEFAULT 0,
        last_seen REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    );
    CREATE INDEX learned_candidates_original ON learned_candidates(original_phrase);
    """,
]  # one script per schema version: a database at user_version n runs the scripts after n


def _clean_context(words: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for word in words:
        word = word.strip()
        if word and normalize(word) not in seen:
            seen.add(normalize(word))
            out.append(word)
    return out


def _cant_keep(path: Path, error) -> str:
    return f"Rflow can't keep your dictionary in {path.parent} ({error}): it works now, but changes won't be kept."


class DictionaryStore:
    """The user's terms, their aliases and the learner's candidates, in SQLite. Thread-safe: one connection, one lock.

    No two terms may share a spelling: an alias that is another term's preferred term or alias is refused with a
    ValueError, because the engine could not tell which term was meant. Spellings are compared by compact_key."""

    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        self._version = 0
        self._data_version = None  # PRAGMA data_version: changes when another connection writes
        self._preferred: dict[str, int] = {}  # compact key of each preferred term -> its id
        try:
            with self._lock:
                if str(path) != ":memory:":
                    self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA foreign_keys=ON")
                current = self._db.execute("PRAGMA user_version").fetchone()[0]
                for version in range(current, len(_SCHEMA)):
                    self._db.executescript(_SCHEMA[version])
                    self._db.execute(f"PRAGMA user_version = {version + 1}")
                    self._db.commit()
                self._sync()
        except Exception:
            self._db.close()  # a damaged file stays locked otherwise, and can't be moved aside
            raise

    @classmethod
    def open(cls, path: str | Path) -> tuple["DictionaryStore", str]:
        """The store at `path`, whatever state it's in, so Rflow always starts: a damaged file is moved aside and a new
        one started; a folder that can't be written gives a store in memory. With what happened, in plain words for
        the user ("" when all is well)."""
        path = Path(path)
        try:
            return cls(path), ""
        except sqlite3.DatabaseError as e:  # not a database, or a damaged one
            damaged = e
        except OSError as e:  # the folder can't be made or written
            log.warning("The dictionary can't be kept in %s: %s", path.parent, e)
            return cls(), _cant_keep(path, e)
        if not path.exists():  # sqlite couldn't create it: the folder isn't writable
            return cls(), _cant_keep(path, damaged)
        aside = path.with_name(f"{path.stem}.damaged-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
        try:
            path.replace(aside)
            for extra in ("-wal", "-shm"):  # its write-ahead log belongs to it
                if (side := path.with_name(path.name + extra)).exists():
                    side.replace(aside.with_name(aside.name + extra))
            store = cls(path)
        except (OSError, sqlite3.Error) as e:
            return cls(), _cant_keep(path, e)
        log.warning("The dictionary was damaged (%s): a new one started, the old one kept as %s", damaged, aside.name)
        return store, (f"Your dictionary file was damaged ({damaged}), so Rflow started a new one: Your words come back "
                       f"from your settings; sound-alikes and learned corrections start again. The old file is kept as "
                       f"{aside.name}.")

    @property
    def version(self) -> int:
        """Goes up with every change to the terms (also one made through another connection to the same file)."""
        with self._lock:
            self._sync()
            return self._version

    def close(self):
        with self._lock:
            self._db.close()

    def _sync(self):
        data_version = self._db.execute("PRAGMA data_version").fetchone()[0]
        if data_version != self._data_version:
            self._data_version = data_version
            self._version += 1
            self._preferred = {compact_key(p): i for i, p in self._db.execute("SELECT id, preferred_term FROM dictionary_terms")}

    def _changed(self):
        self._version += 1

    def _alias_owner(self, key: str) -> int | None:
        row = self._db.execute("SELECT term_id FROM dictionary_aliases WHERE normalized_alias = ?", (key,)).fetchone()
        return row[0] if row else None

    def _name(self, term_id: int) -> str:
        row = self._db.execute("SELECT preferred_term FROM dictionary_terms WHERE id = ?", (term_id,)).fetchone()
        return row[0] if row else f"#{term_id}"

    def _get(self, term_id: int) -> Term | None:
        row = self._db.execute("SELECT id, preferred_term, mode, enabled, source, context_json FROM dictionary_terms "
                               "WHERE id = ?", (term_id,)).fetchone()
        if row is None:
            return None
        aliases = [a for a, in self._db.execute("SELECT alias FROM dictionary_aliases WHERE term_id = ? ORDER BY id", (term_id,))]
        return self._term(row, aliases)

    @staticmethod
    def _term(row, aliases: list[str]) -> Term:
        term_id, preferred, mode, enabled, source, context = row
        try:
            mode = TermMode(mode)
        except ValueError:  # written by a newer Rflow: the cautious mode is a safe reading
            mode = TermMode.CAREFUL
        try:
            context = [str(w) for w in json.loads(context)]
        except (ValueError, TypeError):
            context = []
        return Term(term_id, preferred, aliases, mode, bool(enabled), source, context)

    def _require(self, term_id: int):
        if self._db.execute("SELECT 1 FROM dictionary_terms WHERE id = ?", (term_id,)).fetchone() is None:
            raise KeyError(f"no dictionary term #{term_id}")

    def _check_aliases(self, term_id: int | None, own_key: str, aliases: Iterable[str]) -> list[tuple[str, str]]:
        """The aliases worth adding to a term, as (alias, key); a ValueError if one belongs to another term."""
        out, seen = [], {own_key}
        for alias in aliases:
            alias = alias.strip()
            key = compact_key(alias)
            if not key or key in seen:
                continue  # empty, the term's own spelling, or a repeat: nothing to add
            seen.add(key)
            other = self._preferred.get(key)
            if other is not None and other != term_id:
                raise ValueError(f"alias {alias!r} is the term {self._name(other)!r}")
            owner = self._alias_owner(key)
            if owner is not None and owner != term_id:
                raise ValueError(f"alias {alias!r} already belongs to {self._name(owner)!r}")
            if owner is None:
                out.append((alias, key))
        return out

    def add_term(self, preferred: str, aliases: Iterable[str] = (), mode: TermMode | str = TermMode.CAREFUL,
                 source: str = "user", context: Iterable[str] = ()) -> Term:
        """Add a term, or merge the aliases (and context words) into the existing term spelled the same way."""
        preferred, mode = preferred.strip(), TermMode(mode)
        key = compact_key(preferred)
        if not key:
            raise ValueError("a term needs at least one letter or digit")
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        aliases, context = list(aliases), _clean_context(context)
        with self._lock:
            self._sync()
            term_id = self._preferred.get(key)
            now = time.time()
            if term_id is None:
                owner = self._alias_owner(key)
                if owner is not None:
                    raise ValueError(f"{preferred!r} is already an alias of {self._name(owner)!r}")
                checked = self._check_aliases(None, key, aliases)  # before writing: a bad alias leaves nothing behind
                with self._db:
                    term_id = self._db.execute(
                        "INSERT INTO dictionary_terms (preferred_term, mode, enabled, created_at, updated_at, source, "
                        "context_json) VALUES (?, ?, 1, ?, ?, ?, ?)",
                        (preferred, mode.value, now, now, source, json.dumps(context, ensure_ascii=False))).lastrowid
                    self._db.executemany("INSERT INTO dictionary_aliases (term_id, alias, normalized_alias) VALUES (?, ?, ?)",
                                         [(term_id, a, k) for a, k in checked])
                self._preferred[key] = term_id
            else:
                checked = self._check_aliases(term_id, key, aliases)
                merged = _clean_context(self._get(term_id).context + context)
                with self._db:
                    self._db.executemany("INSERT INTO dictionary_aliases (term_id, alias, normalized_alias) VALUES (?, ?, ?)",
                                         [(term_id, a, k) for a, k in checked])
                    self._db.execute("UPDATE dictionary_terms SET context_json = ?, updated_at = ? WHERE id = ?",
                                     (json.dumps(merged, ensure_ascii=False), now, term_id))
            self._changed()
            return self._get(term_id)

    def update_term(self, term_id: int, *, preferred: str | None = None, mode: TermMode | str | None = None,
                    enabled: bool | None = None, context: Iterable[str] | None = None) -> Term:
        with self._lock:
            self._sync()
            self._require(term_id)
            sets: dict[str, object] = {}
            new_key = drop_alias = None
            if preferred is not None:
                preferred = preferred.strip()
                new_key = compact_key(preferred)
                if not new_key:
                    raise ValueError("a term needs at least one letter or digit")
                other = self._preferred.get(new_key)
                if other is not None and other != term_id:
                    raise ValueError(f"{preferred!r} is already the term #{other}")
                owner = self._alias_owner(new_key)
                if owner is not None and owner != term_id:
                    raise ValueError(f"{preferred!r} is already an alias of {self._name(owner)!r}")
                drop_alias = new_key if owner == term_id else None  # the alias becomes the term's own spelling
                sets["preferred_term"] = preferred
            if mode is not None:
                sets["mode"] = TermMode(mode).value
            if enabled is not None:
                sets["enabled"] = int(bool(enabled))
            if context is not None:
                sets["context_json"] = json.dumps(_clean_context(context), ensure_ascii=False)
            if not sets:
                return self._get(term_id)
            sets["updated_at"] = time.time()
            with self._db:
                if drop_alias:
                    self._db.execute("DELETE FROM dictionary_aliases WHERE term_id = ? AND normalized_alias = ?",
                                     (term_id, drop_alias))
                self._db.execute(f"UPDATE dictionary_terms SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                                 (*sets.values(), term_id))
            if new_key is not None:
                self._preferred = {k: i for k, i in self._preferred.items() if i != term_id}
                self._preferred[new_key] = term_id
            self._changed()
            return self._get(term_id)

    def remove_term(self, term_id: int):
        with self._lock:
            self._sync()
            with self._db:
                self._db.execute("DELETE FROM dictionary_aliases WHERE term_id = ?", (term_id,))
                removed = self._db.execute("DELETE FROM dictionary_terms WHERE id = ?", (term_id,)).rowcount
            if removed:
                self._preferred = {k: i for k, i in self._preferred.items() if i != term_id}
                self._changed()

    def add_alias(self, term_id: int, alias: str) -> Term:
        with self._lock:
            self._sync()
            self._require(term_id)
            own = compact_key(self._name(term_id))
            if not compact_key(alias):
                raise ValueError("an alias needs at least one letter or digit")
            if checked := self._check_aliases(term_id, own, [alias]):
                with self._db:
                    self._db.executemany("INSERT INTO dictionary_aliases (term_id, alias, normalized_alias) VALUES (?, ?, ?)",
                                         [(term_id, a, k) for a, k in checked])
                    self._db.execute("UPDATE dictionary_terms SET updated_at = ? WHERE id = ?", (time.time(), term_id))
                self._changed()
            return self._get(term_id)

    def remove_alias(self, term_id: int, alias: str):
        with self._lock:
            self._sync()
            with self._db:
                removed = self._db.execute("DELETE FROM dictionary_aliases WHERE term_id = ? AND normalized_alias = ?",
                                           (term_id, compact_key(alias))).rowcount
            if removed:
                self._changed()

    def terms(self, enabled_only: bool = True) -> list[Term]:
        with self._lock:
            self._sync()
            aliases: dict[int, list[str]] = {}
            for term_id, alias in self._db.execute("SELECT term_id, alias FROM dictionary_aliases ORDER BY id"):
                aliases.setdefault(term_id, []).append(alias)
            rows = self._db.execute("SELECT id, preferred_term, mode, enabled, source, context_json FROM dictionary_terms"
                                    + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY id").fetchall()
            return [self._term(row, aliases.get(row[0], [])) for row in rows]

    def find(self, preferred: str) -> Term | None:
        """The term spelled `preferred` (compared by compact_key: case, spaces and hyphens don't matter)."""
        with self._lock:
            self._sync()
            term_id = self._preferred.get(compact_key(preferred))
            return self._get(term_id) if term_id is not None else None

    def sync_vocabulary(self, words: Iterable[str]):
        """Mirror the app's "Your words": each word becomes a CAREFUL term from the vocabulary, and vocabulary terms no
        longer in the list go. Terms the user added here, or learned ones, are never touched; a word already covered by
        one of them (as its spelling or an alias) is left to it."""
        wanted: dict[str, str] = {}
        for word in words:
            word = word.strip()
            if (key := compact_key(word)) and key not in wanted:
                wanted[key] = word
        with self._lock:
            self._sync()
            changed = False
            rows = self._db.execute("SELECT id, preferred_term, source FROM dictionary_terms").fetchall()
            now = time.time()
            with self._db:
                for term_id, preferred, source in rows:
                    key = compact_key(preferred)
                    if source != "vocabulary":
                        continue
                    if key not in wanted:
                        self._db.execute("DELETE FROM dictionary_aliases WHERE term_id = ?", (term_id,))
                        self._db.execute("DELETE FROM dictionary_terms WHERE id = ?", (term_id,))
                        self._preferred.pop(key, None)
                        changed = True
                    elif wanted[key] != preferred:  # the user changed its casing in the list
                        self._db.execute("UPDATE dictionary_terms SET preferred_term = ?, updated_at = ? WHERE id = ?",
                                         (wanted[key], now, term_id))
                        changed = True
                for key, word in wanted.items():
                    if key in self._preferred or self._alias_owner(key) is not None:
                        continue
                    self._preferred[key] = self._db.execute(
                        "INSERT INTO dictionary_terms (preferred_term, mode, enabled, created_at, updated_at, source, "
                        "context_json) VALUES (?, ?, 1, ?, ?, 'vocabulary', '[]')",
                        (word, TermMode.CAREFUL.value, now, now)).lastrowid
                    changed = True
            if changed:
                self._changed()

    def hint_terms(self) -> list[str]:
        """The preferred spelling of every enabled term, whatever its mode: for engines that take vocabulary hints."""
        return [term.preferred for term in self.terms()]


# ---------------------------------------------------------------- the engine

@dataclass(slots=True, eq=False)
class _Form:
    """One spelling the engine looks for: a term's preferred spelling or one of its aliases."""
    term: Term
    text: str
    key: str
    ntokens: int
    alias: bool


@dataclass(slots=True)
class _Hit:
    i: int  # tokens [i, j)
    j: int
    end: int  # character end of the replaced span (before a kept "'s" or plural "s")
    form: _Form
    score: float


class DictionaryEngine:
    """Applies a DictionaryStore's terms to text (correct). Builds its index on first use and again whenever the store
    changes; correct() is safe to call from one thread at a time per engine."""

    def __init__(self, store: DictionaryStore, config: DictionaryConfig | None = None):
        self.store = store
        self.config = config or DictionaryConfig()
        self._built = None  # the store version the index was built from
        self._exact: dict[str, _Form] = {}  # compact key -> form, every enabled term (HINT_ONLY too: it protects)
        # Candidate retrieval for fuzzy matching, so a window is scored against a handful of forms, not the whole
        # dictionary: forms whose sound code, or whose spelling, is at most one edit from the window's.
        self._fuzzy: dict[str, list[_Form]] = {}  # deletion of a phonetic code -> forms
        self._spelled: dict[str, list[_Form]] = {}  # deletion of a compact key -> forms
        self._context: dict[int, list[str]] = {}  # term id -> compact keys of its context words
        self._longest = 1  # the most words a form has

    def _refresh(self):
        version = self.store.version
        if version == self._built:
            return
        exact, fuzzy, spelled, context, longest = {}, {}, {}, {}, 1
        for term in self.store.terms(enabled_only=True):
            context[term.id] = [k for k in (compact_key(w) for w in term.context) if k]
            for text, alias in [(term.preferred, False)] + [(a, True) for a in term.aliases]:
                tokens = tokenize(text, protect=False)
                key = "".join(t.key for t in tokens)
                if not key or key in exact:
                    continue
                form = exact[key] = _Form(term, text, key, len(tokens), alias)
                longest = max(longest, len(tokens))
                sound = phonetic(key)
                if term.mode is not TermMode.HINT_ONLY and len(key) >= MIN_FUZZY_LETTERS and key.isascii() \
                        and key.isalpha() and len(sound) >= 2:
                    for k in _deletions(sound):
                        fuzzy.setdefault(k, []).append(form)
                    for k in _deletions(key):
                        spelled.setdefault(k, []).append(form)
        self._exact, self._fuzzy, self._spelled, self._context = exact, fuzzy, spelled, context
        self._longest, self._built = longest, version

    def correct(self, text: str) -> DictionaryResult:
        if not self.config.enabled or not text.strip():
            return DictionaryResult(text)
        self._refresh()
        if not self._exact:
            return DictionaryResult(text)
        tokens = tokenize(text)
        joins = [joinable(text[a.end:b.start]) for a, b in zip(tokens, tokens[1:], strict=False)]
        common = [is_common(t.norm) for t in tokens]
        claimed = [False] * len(tokens)
        hits: list[tuple[_Hit, str]] = []
        rejected: list[tuple[int, int, str]] = []
        self._exact_pass(text, tokens, joins, common, claimed, hits, rejected)
        if self._fuzzy and common_words():  # without the ordinary-word list fuzzy matching would be reckless
            self._fuzzy_pass(text, tokens, joins, common, claimed, hits, rejected)
        replacements = []
        for hit, kind in sorted(hits, key=lambda h: tokens[h[0].i].start):
            start = tokens[hit.i].start
            replacements.append(DictionaryReplacement(start, hit.end, text[start:hit.end], hit.form.term.preferred,
                                                      hit.form.term.preferred, kind, round(hit.score, 3)))
        parts, pos = [], 0
        for r in replacements:
            parts += [text[pos:r.start], r.replacement]
            pos = r.end
        parts.append(text[pos:])
        notes = [note for i, j, note in rejected if not any(claimed[i:j])]
        return DictionaryResult("".join(parts), replacements, list(dict.fromkeys(notes)))

    # ------------------------------------------------------------ exact: the term's spelling or an alias

    def _suffixed(self, prefix: str, token: Token, common: bool) -> tuple[_Form | None, int]:
        """A form followed by a possessive ("open ai's") or, for a word that isn't ordinary, a plural "s" ("apis")."""
        raw = token.text
        if raw.endswith(_POSSESSIVE) and len(raw) > 2:
            cut = 2
        elif raw[-1:] in ("s", "S") and len(token.key) >= 3 and not common:  # "its" is never "IT" + s
            cut = 1
        else:
            return None, token.end
        form = self._exact.get(prefix + _compact(normalize(raw[:-cut])))
        return form, token.end - cut

    def _exact_pass(self, text, tokens, joins, common, claimed, hits, rejected):
        found = []
        for i in range(len(tokens)):
            prefix = ""
            for j in range(i, min(len(tokens), i + self._longest + 2)):  # +2: a one-word term said as three words
                if j > i and not joins[j - 1]:
                    break
                key = prefix + tokens[j].key
                form, end = self._exact.get(key), tokens[j].end
                if form is None:
                    form, end = self._suffixed(prefix, tokens[j], common[j])
                if form is not None:
                    found.append(_Hit(i, j + 1, end, form, 1.0))
                prefix = key
        # Longest first (in words, then characters): "visual studio code" beats "visual studio", "open ai" beats "ai".
        found.sort(key=lambda h: (-(h.j - h.i), -(h.end - tokens[h.i].start), h.i))
        for hit in found:
            if any(claimed[hit.i:hit.j]):
                continue
            verdict = self._exact_verdict(text, tokens, common, hit)
            if verdict in ("keep", "case", "alias"):
                claimed[hit.i:hit.j] = [True] * (hit.j - hit.i)
                if verdict != "keep":
                    hits.append((hit, verdict))
            else:
                original = text[tokens[hit.i].start:hit.end]
                rejected.append((hit.i, hit.j, f'"{original}" -> "{hit.form.term.preferred}" ({verdict})'))

    def _exact_verdict(self, text, tokens, common, hit) -> str:
        """"keep" (already right, or HINT_ONLY), "case", "alias", or why it isn't applied."""
        form, term = hit.form, hit.form.term
        if term.mode is TermMode.HINT_ONLY or (not form.alias and text[tokens[hit.i].start:hit.end] == term.preferred):
            return "keep"
        n = hit.j - hit.i
        ordinary = all(common[hit.i:hit.j])
        careful_without_context = term.mode is TermMode.CAREFUL and not self._has_context(term, tokens, hit.i, hit.j)
        if n != form.ntokens:  # said with other spacing: "java script", "openai", "post-grass"
            if any(common[k] and len(tokens[k].key) == 1 for k in range(hit.i, hit.j)):
                return 'a one-letter word joined to others, as in "a pi" or "I phone"'
            if ordinary and is_common(form.key):
                return 'ordinary words that together make an ordinary word, as in "now here"'
            if ordinary and careful_without_context:
                return "ordinary words, a careful term and no context word"
        if not form.alias:
            if n == 1 and ordinary:
                return "an ordinary word: its casing is left alone"
            return "case"
        if n == 1 and ordinary and careful_without_context:
            return "an ordinary word as the alias of a careful term, and no context word"
        return "alias"

    def _has_context(self, term: Term, tokens: list[Token], i: int, j: int) -> bool:
        words = self._context.get(term.id)
        if not words:
            return False
        w = self.config.context_window
        for token in tokens[max(0, i - w):i] + tokens[j:j + w]:
            for word in words:  # "cluster" also supports in "clusters"
                if token.key == word or (len(word) >= 4 and token.key.startswith(word) and len(token.key) - len(word) <= 3):
                    return True
        return False

    # ------------------------------------------------------------ fuzzy: near misses by spelling and sound

    def _threshold(self, term: Term, tokens, i, j) -> tuple[float, str]:
        cfg = self.config
        base = cfg.fuzzy_threshold if term.mode is TermMode.AUTOMATIC else cfg.careful_threshold
        if self._has_context(term, tokens, i, j):
            return min(base, cfg.context_threshold), f"{term.mode.value}, context word near"
        return base, term.mode.value

    def _fuzzy_pass(self, text, tokens, joins, common, claimed, hits, rejected):
        found: list[_Hit] = []
        for i in range(len(tokens)):
            prefix = ""
            for j in range(i, min(len(tokens), i + MAX_FUZZY_TOKENS)):
                token = tokens[j]
                if claimed[j] or (j > i and not joins[j - 1]) or token.key in self._exact:
                    break  # another term's exact spelling is never turned into this one
                key, end = token.key, token.end
                possessive = token.text.endswith(_POSSESSIVE) and len(token.text) > 2
                if possessive:
                    key, end = _compact(normalize(token.text[:-2])), token.end - 2
                    if key in self._exact:
                        break
                if not (key.isascii() and key.isalpha()):
                    break  # digits, symbols or another script: the sound code is for English words only
                key = prefix + key
                if (len(key) >= MIN_FUZZY_LETTERS and not all(common[i:j + 1])
                        and (j == i or (tokens[i].norm not in STOPWORDS and token.norm not in STOPWORDS))):
                    if hit := self._fuzzy_hit(key, tokens, i, j + 1, end, rejected):
                        found.append(hit)
                if possessive:
                    break  # "'s" ends a name: no phrase goes on past it
                prefix = key
        found.sort(key=lambda h: (-h.score, -(h.j - h.i), h.i))
        for hit in found:
            if not any(claimed[hit.i:hit.j]):
                claimed[hit.i:hit.j] = [True] * (hit.j - hit.i)
                hits.append((hit, "fuzzy"))

    def _fuzzy_hit(self, key, tokens, i, j, end, rejected) -> _Hit | None:
        candidates = {}
        for index, code in ((self._fuzzy, phonetic(key)), (self._spelled, key)):
            for k in _deletions(code):
                for form in index.get(k, ()):
                    candidates[id(form)] = form
        best: dict[int, tuple[float, _Form]] = {}  # term id -> its best form
        for form in candidates.values():
            if min(len(key), len(form.key)) / max(len(key), len(form.key)) < MIN_LENGTH_RATIO:
                continue
            score = similarity(key, form.key)
            if score > best.get(form.term.id, (-1.0, None))[0]:
                best[form.term.id] = (score, form)
        if not best:
            return None
        ranked = sorted(best.values(), key=lambda s: -s[0])
        score, form = ranked[0]
        original = " ".join(t.text for t in tokens[i:j])
        threshold, why = self._threshold(form.term, tokens, i, j)
        if score < threshold:
            if score >= REPORT_FLOOR:
                rejected.append((i, j, f'"{original}" -> "{form.term.preferred}" (fuzzy {score:.3f} < {threshold:.2f}, {why})'))
            return None
        if len(ranked) > 1 and ranked[1][0] >= score - AMBIGUITY_MARGIN:
            rejected.append((i, j, f'"{original}" -> "{form.term.preferred}" or "{ranked[1][1].term.preferred}" '
                                   f"(fuzzy {score:.3f}, ambiguous)"))
            return None
        return _Hit(i, j, end, form, score)
