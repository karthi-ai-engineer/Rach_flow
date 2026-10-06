"""The dictionary stage: the user's terms replace known recognition mistakes, and nothing else. Most tests here are about
what must NOT change: a wrong replacement silently changes what the user said."""
import random
import sqlite3
import time

import pytest

from sst.pipeline.contracts import DictionaryConfig
from sst.pipeline.dictionary import (
    COMMON_WORDS_FILE,
    DictionaryEngine,
    DictionaryStore,
    TermMode,
    common_words,
    compact_key,
    is_common,
    phonetic,
    similarity,
    tokenize,
)

AUTO, CAREFUL, HINT = TermMode.AUTOMATIC, TermMode.CAREFUL, TermMode.HINT_ONLY


@pytest.fixture
def store():
    s = DictionaryStore()
    yield s
    s.close()


def fix(store, text, **config):
    return DictionaryEngine(store, DictionaryConfig(**config)).correct(text)


def check_spans(text, result):
    """Every replacement's span is in the input, the spans don't overlap, and everything outside them is kept."""
    pos, rebuilt = 0, []
    for r in result.replacements:
        assert text[r.start:r.end] == r.original and r.start >= pos
        rebuilt += [text[pos:r.start], r.replacement]
        pos = r.end
    assert "".join(rebuilt) + text[pos:] == result.text


# ---------------------------------------------------------------- the plan's examples (§101)

def test_open_ai_becomes_openai_however_it_is_spaced(store):
    store.add_term("OpenAI", ["open ai"], mode=AUTO)
    for said in ("open ai", "Open AI", "open-ai", "openai", "OPEN AI"):
        result = fix(store, f"ask {said} today")
        assert result.text == "ask OpenAI today", said
        check_spans(f"ask {said} today", result)


def test_post_grass_is_an_alias_and_the_punctuation_stays(store):
    store.add_term("PostgreSQL", ["post grass", "post gres", "postgress"], mode=AUTO)
    result = fix(store, "Post grass, please.")
    assert result.text == "PostgreSQL, please."
    [r] = result.replacements
    assert (r.start, r.end, r.original, r.replacement, r.term, r.kind) == (0, 10, "Post grass", "PostgreSQL", "PostgreSQL",
                                                                           "alias")
    assert fix(store, "is postgress up? Post-Gres is.").text == "is PostgreSQL up? PostgreSQL is."


def test_javascript_gets_its_casing(store):
    store.sync_vocabulary(["JavaScript"])  # a word from "Your words": CAREFUL
    result = fix(store, "I write javascript and Java Script daily")
    assert result.text == "I write JavaScript and JavaScript daily"
    assert [r.kind for r in result.replacements] == ["case", "case"]


def test_ordinary_words_are_never_tempted(store):
    # Terms chosen to be as close as possible to the ordinary words in the text, all AUTOMATIC.
    store.add_term("OpenAI", ["open ai"], mode=AUTO)
    store.add_term("PostgreSQL", ["post grass", "post gres", "postgress"], mode=AUTO)
    store.add_term("Katalog", mode=AUTO)  # "catalog" scores 0.886 against it
    store.add_term("Postofice", mode=AUTO)  # "post office" written together scores 0.9
    store.add_term("Concat", mode=AUTO)
    text = "The catalog lists the cat. We concatenate files. Dinner in the open air, then the post office."
    result = fix(store, text)
    assert result.text == text and not result.replacements


def test_whole_words_only(store):
    store.add_term("Kat", ["cat"], mode=AUTO)  # an explicit rule, even for an ordinary word
    assert fix(store, "my cat can concatenate a catalog of bobcats").text == "my Kat can concatenate a catalog of bobcats"


# ---------------------------------------------------------------- phrases, suffixes, punctuation

def test_possessives_and_plurals_keep_their_ending(store):
    store.add_term("OpenAI", ["open ai"], mode=AUTO)
    store.add_term("GitHub")
    store.add_term("API")
    assert fix(store, "open ai's model").text == "OpenAI's model"
    assert fix(store, "Open AI\u2019s model").text == "OpenAI\u2019s model"
    assert fix(store, "github's issues and two apis").text == "GitHub's issues and two APIs"


def test_its_is_never_a_plural_of_a_term(store):
    store.add_term("IT", mode=AUTO)
    assert fix(store, "its fine, it is").text == "its fine, it is"


def test_multi_word_terms(store):
    store.add_term("Visual Studio Code")
    store.add_term("Amazon Web Services", ["amazon web service"], mode=AUTO)
    result = fix(store, "open visual studio code, then amazon web service.")
    assert result.text == "open Visual Studio Code, then Amazon Web Services."
    check_spans("open visual studio code, then amazon web service.", result)


def test_the_longest_match_wins(store):
    store.add_term("Visual Studio")
    store.add_term("Visual Studio Code")
    store.add_term("OpenAI", ["open ai"], mode=AUTO)
    store.add_term("AI", mode=AUTO)
    assert fix(store, "visual studio code").text == "Visual Studio Code"
    assert fix(store, "visual studio is big").text == "Visual Studio is big"
    result = fix(store, "open ai and ai")
    assert result.text == "OpenAI and AI"
    check_spans("open ai and ai", result)


def test_no_phrase_crosses_sentence_punctuation(store):
    store.add_term("OpenAI", ["open ai"], mode=AUTO)
    store.add_term("PostgreSQL", ["post grass"], mode=AUTO)
    for text in ("Leave it open. Ai is next", "open, ai", "open; ai", "open! AI", "open? ai", "post. Grass grows",
                 "open (ai)", 'open "ai"', "open\nai"):
        assert fix(store, text).text == text, text


def test_everything_outside_a_replacement_is_kept(store):
    store.add_term("PostgreSQL", ["post grass"], mode=AUTO)
    text = '  "post grass"\n\tthen   POST GRASS…  '
    result = fix(store, text)
    assert result.text == '  "PostgreSQL"\n\tthen   PostgreSQL…  '
    check_spans(text, result)


def test_a_correct_term_is_left_alone(store):
    store.add_term("GitHub")
    result = fix(store, "GitHub, GitHub's and GitHubs")
    assert result.text == "GitHub, GitHub's and GitHubs" and not result.replacements


def test_urls_emails_and_identifiers_are_untouched(store):
    store.add_term("OpenAI", ["open ai"], mode=AUTO)
    store.add_term("PostgreSQL", ["post grass"], mode=AUTO)
    text = "see https://github.com/openai/post-grass, mail openai@example.com, run open_ai.py or C:\\openai\\x"
    assert fix(store, text).text == text


def test_an_ordinary_word_keeps_its_casing(store):
    store.sync_vocabulary(["Swift", "Mark", "Rust"])  # names and languages that are also ordinary words
    result = fix(store, "a swift reply: mark it, rust never sleeps")
    assert result.text == "a swift reply: mark it, rust never sleeps"
    assert any("swift" in note for note in result.rejected)


def test_ordinary_words_said_apart_are_not_joined_into_a_term(store):
    store.add_term("API", mode=AUTO)
    store.add_term("iPhone", mode=AUTO)
    store.add_term("Nowhere", mode=AUTO)
    store.add_term("TypeScript")  # CAREFUL
    for text in ("a pi chart", "I phone my mother", "I am now here", "type script by hand"):
        assert fix(store, text).text == text, text
    store.update_term(store.find("TypeScript").id, mode=AUTO)
    assert fix(store, "type script by hand").text == "TypeScript by hand"


def test_a_careful_alias_that_is_an_ordinary_word_needs_context(store):
    claude = store.add_term("Claude", ["cloud"], context=["model", "prompt"])
    assert fix(store, "upload it to the cloud").text == "upload it to the cloud"
    assert fix(store, "ask the cloud model").text == "ask the Claude model"
    store.update_term(claude.id, mode=AUTO)
    assert fix(store, "upload it to the cloud").text == "upload it to the Claude"  # the user asked for it


def test_the_preferred_casing_is_used_whatever_was_said(store):
    store.add_term("PostgreSQL", ["Post Grass"], mode=AUTO)
    assert fix(store, "POST GRASS and post grass").text == "PostgreSQL and PostgreSQL"


# ---------------------------------------------------------------- fuzzy (§102: obvious, near, borderline, wrong)

def test_fuzzy_obvious(store):
    store.add_term("Terraform", mode=AUTO)
    store.add_term("Elasticsearch")  # CAREFUL: needs 0.92, and gets 0.923
    result = fix(store, "run teraform, then query elasticserch")
    assert result.text == "run Terraform, then query Elasticsearch"
    assert [r.kind for r in result.replacements] == ["fuzzy", "fuzzy"]
    assert all(r.score >= 0.88 for r in result.replacements)


def test_fuzzy_finds_a_close_spelling_that_sounds_different(store):
    # One letter off, but "sh" changes the sound code by two symbols: only the spelling index retrieves it.
    assert phonetic("elashicsearch") != phonetic("elasticsearch")
    store.add_term("Elasticsearch")
    store.add_term("Prometheus", mode=AUTO)
    assert fix(store, "query elashicsearch and phometheus").text == "query Elasticsearch and Prometheus"


def test_fuzzy_near(store):
    store.add_term("Kubernetes", mode=AUTO)
    result = fix(store, "the kubernets cluster and the kuberneties pods")
    assert result.text == "the Kubernetes cluster and the Kubernetes pods"


def test_fuzzy_borderline_depends_on_context(store):
    store.add_term("Kubernetes", context=["cluster"])  # CAREFUL
    assert fix(store, "deploy the kuberneties cluster").text == "deploy the Kubernetes cluster"
    result = fix(store, "the old word was kuberneties")
    assert result.text == "the old word was kuberneties"
    [note] = result.rejected
    assert "kuberneties" in note and "Kubernetes" in note
    assert 0.84 <= similarity("kuberneties", "kubernetes") < DictionaryConfig().careful_threshold


def test_fuzzy_sound_alike_reaches_the_term_only_with_context(store):
    assert phonetic("kubernetes") == phonetic("kuberneties") == phonetic("coopernetties") == phonetic("cubeernetease")
    store.add_term("Kubernetes", context=["cluster"])
    for said in ("cooper netties", "cube er net ease"):
        assert fix(store, f"scale the {said} cluster").text == "scale the Kubernetes cluster", said
        assert fix(store, f"I met {said} yesterday").text == f"I met {said} yesterday", said
    store.update_term(store.find("Kubernetes").id, context=[], mode=AUTO)
    assert fix(store, "scale the cooper netties").text == "scale the cooper netties"  # sound alone isn't enough


def test_fuzzy_wrong_matches_are_left(store):
    store.add_term("Kubernetes", mode=AUTO)
    store.add_term("Terraform", mode=AUTO)
    text = "governance of the cabinets, kombucha and cabernets, the terrace and the uniform"
    assert fix(store, text).text == text


def test_fuzzy_never_touches_short_words(store):
    store.add_term("Bun", mode=AUTO)
    store.add_term("Deno", mode=AUTO)
    store.add_term("Vite", mode=AUTO)
    assert fix(store, "a bon vie, dino and vit").text == "a bon vie, dino and vit"


def test_fuzzy_never_turns_one_term_into_another(store):
    store.add_term("Kotlin", mode=AUTO)
    store.add_term("Katlin", mode=CAREFUL)  # a colleague
    store.add_term("Kubernetes", mode=AUTO)
    store.add_term("Kubernetis", mode=HINT)
    assert fix(store, "ask katlin about kubernetis").text == "ask Katlin about kubernetis"


def test_fuzzy_ambiguity_is_left_alone(store):
    store.add_term("Karthik", mode=AUTO)
    store.add_term("Karthic", mode=AUTO)
    result = fix(store, "thanks karthick")
    assert result.text == "thanks karthick"
    assert any("ambiguous" in note for note in result.rejected)


def test_fuzzy_keeps_a_possessive(store):
    store.add_term("Kubernetes", mode=AUTO)
    assert fix(store, "kubernets's API").text == "Kubernetes's API"


def test_hint_only_never_replaces(store):
    store.add_term("PostgreSQL", ["post grass"], mode=HINT)
    text = "post grass, postgresql and postgress"
    assert fix(store, text).text == text
    assert store.hint_terms() == ["PostgreSQL"]


def test_disabled_terms_and_a_disabled_stage_do_nothing(store):
    term = store.add_term("OpenAI", ["open ai"], mode=AUTO)
    assert fix(store, "open ai", enabled=False).text == "open ai"
    store.update_term(term.id, enabled=False)
    assert fix(store, "open ai").text == "open ai"
    assert store.terms() == [] and len(store.terms(enabled_only=False)) == 1
    assert store.hint_terms() == []


def test_unicode_text_is_untouched(store):
    store.add_term("OpenAI", ["open ai"], mode=AUTO)
    store.add_term("Kubernetes", mode=AUTO)
    store.add_term("தமிழ்")
    for text in ("நான் தமிழ் பேசுகிறேன்", "東京でオープンAIを使う", "Ｋｕｂｅｒｎｅｔｅｓ だ", "naïve café déjà vu"):
        result = fix(store, text)
        assert result.text == text or result.text == "Kubernetes だ", text
    assert fix(store, "நான் தமிழ் பேசுகிறேன்").replacements == []


def test_empty_text_and_empty_dictionary(store):
    assert fix(store, "").text == ""
    assert fix(store, "nothing to do").text == "nothing to do"
    store.add_term("OpenAI", ["open ai"])
    assert fix(store, "   ").text == "   "


# ---------------------------------------------------------------- the store

def test_add_term_merges_into_the_same_spelling(store):
    first = store.add_term("OpenAI", ["open ai", "open a eye"], mode=AUTO, context=["model"])
    assert first.aliases == ["open a eye"]  # "open ai" is the term's own spelling, matched anyway
    again = store.add_term("openai", ["open eye", "Open A Eye"], context=["Model", "api"])
    assert again.id == first.id and again.preferred == "OpenAI" and again.mode is AUTO
    assert again.aliases == ["open a eye", "open eye"]
    assert again.context == ["model", "api"]
    assert store.find("Open-AI").id == first.id and store.find("nothing") is None


def test_ambiguous_spellings_are_refused(store):
    pg = store.add_term("PostgreSQL", ["post grass"])
    with pytest.raises(ValueError):
        store.add_term("Postgres", ["post grass"])
    assert store.find("Postgres") is None  # nothing half-added
    with pytest.raises(ValueError):
        store.add_term("Post-Grass")  # already an alias
    other = store.add_term("MySQL")
    with pytest.raises(ValueError):
        store.add_alias(other.id, "postgresql")  # another term's spelling
    with pytest.raises(ValueError):
        store.add_alias(other.id, "POST GRASS")  # another term's alias
    with pytest.raises(ValueError):
        store.update_term(other.id, preferred="post grass")
    with pytest.raises(ValueError):
        store.add_alias(other.id, "  ")
    with pytest.raises(KeyError):
        store.add_alias(999, "x")
    renamed = store.update_term(pg.id, preferred="Post Grass")  # its own alias may become its spelling
    assert renamed.preferred == "Post Grass" and renamed.aliases == []


def test_aliases_can_be_added_and_removed(store):
    term = store.add_term("PostgreSQL")
    assert store.add_alias(term.id, "post grass").aliases == ["post grass"]
    assert store.add_alias(term.id, "Post-Grass").aliases == ["post grass"]  # the same alias
    assert fix(store, "post grass").text == "PostgreSQL"
    store.remove_alias(term.id, "POST GRASS")
    assert store.find("PostgreSQL").aliases == []
    assert fix(store, "post grass").text == "post grass"


def test_sync_vocabulary_mirrors_your_words(store):
    store.add_term("PostgreSQL", ["post grass"], mode=AUTO)  # the user's own
    store.add_term("Claude", ["cloud"], source="learned")
    store.sync_vocabulary(["Kubernetes", "Tamil", "postgresql", "Post Grass", "  ", "tamil"])
    terms = {t.preferred: t for t in store.terms()}
    assert set(terms) == {"PostgreSQL", "Claude", "Kubernetes", "Tamil"}
    assert terms["Kubernetes"].source == "vocabulary" and terms["Kubernetes"].mode is CAREFUL
    assert terms["PostgreSQL"].source == "user" and terms["PostgreSQL"].mode is AUTO
    version = store.version
    store.sync_vocabulary(["Kubernetes", "Tamil"])
    assert store.version == version  # nothing changed
    store.sync_vocabulary(["kubernetes"])  # Tamil removed, Kubernetes re-cased
    assert {t.preferred: t.source for t in store.terms()} == {"PostgreSQL": "user", "Claude": "learned",
                                                              "kubernetes": "vocabulary"}
    store.sync_vocabulary([])
    assert {t.preferred for t in store.terms()} == {"PostgreSQL", "Claude"}


def test_the_store_survives_reopening(tmp_path):
    path = tmp_path / "profile" / "dictionary.sqlite3"
    store = DictionaryStore(path)
    store.add_term("PostgreSQL", ["post grass"], mode=AUTO, context=["database"])
    store.add_term("Kubernetes", mode=HINT)
    off = store.add_term("Rflow")
    store.update_term(off.id, enabled=False)
    before = store.terms(enabled_only=False)
    store.close()
    reopened = DictionaryStore(path)
    assert reopened.terms(enabled_only=False) == before
    assert fix(reopened, "post grass").text == "PostgreSQL"
    reopened.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"dictionary_terms", "dictionary_aliases", "learned_candidates"} <= tables


def test_the_engine_rebuilds_when_the_store_changes(store):
    engine = DictionaryEngine(store)
    version = store.version
    term = store.add_term("OpenAI", mode=AUTO)
    assert store.version > version
    assert engine.correct("open ai").text == "OpenAI"  # by its spelling
    store.add_alias(term.id, "open eye")
    assert engine.correct("open eye").text == "OpenAI"
    store.update_term(term.id, mode=HINT)
    assert engine.correct("open eye").text == "open eye"
    store.update_term(term.id, mode=AUTO)
    store.remove_term(term.id)
    assert engine.correct("open eye").text == "open eye"
    assert store.terms(enabled_only=False) == []


def test_the_version_sees_another_connection(tmp_path):
    path = tmp_path / "dictionary.sqlite3"
    app, pipeline = DictionaryStore(path), DictionaryStore(path)
    engine = DictionaryEngine(pipeline)
    assert engine.correct("post grass").text == "post grass"
    version = pipeline.version
    app.add_term("PostgreSQL", ["post grass"], mode=AUTO)
    assert pipeline.version > version
    assert engine.correct("post grass").text == "PostgreSQL"
    app.close()
    pipeline.close()


# ---------------------------------------------------------------- the building blocks

def test_tokens_keep_their_spans_and_compare_normalized():
    text = "“Open-AI’s” C++ and node.js, ＡＩ"
    tokens = tokenize(text)
    assert [t.text for t in tokens] == ["Open", "AI’s", "C++", "and", "ＡＩ"]  # node.js looks like a domain: protected
    assert all(text[t.start:t.end] == t.text for t in tokens)
    assert [t.key for t in tokens] == ["open", "ai's".replace("'", ""), "c++", "and", "ai"]
    assert compact_key("Open AI") == compact_key("open-ai") == compact_key("OPENAI") == "openai"
    assert compact_key("C#") != compact_key("C")


def test_common_words_list():
    words = COMMON_WORDS_FILE.read_text(encoding="utf-8").split()
    assert len(words) >= 2500 and len(set(words)) == len(words) and words == sorted(words)
    assert all(w == w.lower() for w in words)
    assert {"open", "air", "post", "office", "catalog", "the", "running", "children", "don't"} <= common_words()
    assert not {"ai", "api", "er", "cooper", "kubernetes", "github", "postgres"} & set(words)
    assert is_common("offices") and is_common("studied") and not is_common("kuberneties") and not is_common("netties")


def test_performance_5000_terms_200_words():
    rng = random.Random(7)
    syllables = ["ka", "zo", "ri", "mu", "tel", "vor", "quin", "bex", "lan", "dra", "pho", "sti", "gor", "nex", "ul",
                 "yar", "fen", "wix", "tra", "mol", "sep", "dun", "cri", "vel"]

    def word():
        return "".join(rng.choice(syllables) for _ in range(rng.randint(2, 4))).capitalize()

    store = DictionaryStore()
    names = []
    while len(names) < 5000:
        name = word() if rng.random() < 0.7 else f"{word()} {word()}"
        try:
            store.add_term(name, [name.lower().replace("a", "e", 1)] if rng.random() < 0.5 else [],
                           mode=rng.choice([AUTO, CAREFUL, HINT]), context=["cluster"] if rng.random() < 0.2 else [])
        except ValueError:
            continue
        names.append(name)
    prose = ("so yesterday I talked with the team about the release and we decided to move the deploy to friday "
             "because the database migration still needs a review and nobody wants to rush it before the weekend "
             "then we looked at the dashboard together and saw that the error rate went up after the last change "
             "which probably means the cache is not warming correctly so I will open a ticket and ask for help").split()
    picked = [n.lower() for n in rng.sample(names, 12)] + [names[0].lower()[:-1] + "x", names[1].lower() + "s"]
    words = prose[:]
    while len(words) < 200:
        words.insert(rng.randrange(len(words)), rng.choice(picked) if rng.random() < 0.3 else rng.choice(prose))
    text = " ".join(words[:200]) + "."
    engine = DictionaryEngine(store)
    engine.correct(text)  # builds the index
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        result = engine.correct(text)
        timings.append(time.perf_counter() - start)
    check_spans(text, result)
    assert min(timings) < 0.050, timings
    store.close()



def test_only_names_and_terms_are_speech_hints():
    from sst.pipeline.dictionary import speech_hints
    owner = ["version", "installer", "commit", "tray", "app", "pill", "Let's", "move", "stand", "meeting", "Thursday",
             "morning", "commits", "GitHub", "Rflow", "Parakeet", "laptop", "Vercel", "after", "tests"]
    assert speech_hints(owner) == ["tray", "GitHub", "Rflow", "Parakeet", "Vercel"]
    assert speech_hints(["Visual Studio Code", "stand meeting", "Karthi", "karthi", "H100", " ", "Swift"]) == [
        "Visual Studio Code", "Karthi", "H100"]  # a name written as one stays; duplicates and everyday words go


def test_a_damaged_dictionary_is_moved_aside_and_a_new_one_starts(tmp_path):
    from sst.pipeline.dictionary import DictionaryStore
    path = tmp_path / "dictionary.db"
    path.write_bytes(b"this is not a database" * 100)
    store, problem = DictionaryStore.open(path)
    store.add_term("Kubernetes")  # it works
    store.close()
    assert problem.startswith("Your dictionary file was damaged") and "Your words come back from your settings" in problem
    assert len(list(tmp_path.glob("dictionary.damaged-*.db"))) == 1


def test_a_dictionary_that_cant_be_written_works_in_memory(tmp_path):
    from sst.pipeline.dictionary import DictionaryStore
    blocker = tmp_path / "profiles"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    store, problem = DictionaryStore.open(blocker / "dictionary.db")
    store.add_term("Kubernetes")
    assert problem.startswith("Rflow can't keep your dictionary in") and "won't be kept" in problem


def test_a_good_dictionary_opens_without_a_word(tmp_path):
    from sst.pipeline.dictionary import DictionaryStore
    store, problem = DictionaryStore.open(tmp_path / "dictionary.db")
    store.close()
    assert problem == ""
