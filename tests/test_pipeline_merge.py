"""The merge stage: chunk transcripts become one, with the words of each overlap second kept once. The plan's §100 cases
(exact, case, punctuation, one-word mismatch, fuzzy, no overlap, false overlap), partial words at a forced cut (§97),
timestamps for and against a duplicate, progressive merging, odd chunks, other scripts and speed."""
import time

import pytest

from sst.pipeline.contracts import ChunkingConfig, ChunkResult, WordInfo
from sst.pipeline.merge import Token, TranscriptMerger, merge_boundary, normalize, tokenize


def chunk(seq, text, words=None, start=None, end=None, overlap_end=None):
    """Chunk `seq` of a session cut every 19 s: [19(seq-1), +20), the first second repeating the previous chunk's end."""
    start = 19.0 * (seq - 1) if start is None else start
    end = start + 20.0 if end is None else end
    overlap_end = (start + 1.0 if seq > 1 else start) if overlap_end is None else overlap_end
    return ChunkResult("sess_test", seq, start, end, overlap_end, text, words or [])


def timed(seq, spec, **kw):
    """A chunk whose engine gave word times: spec is [(word, start, end)] in session seconds."""
    return chunk(seq, " ".join(w for w, _, _ in spec), [WordInfo(w, s, e) for w, s, e in spec], **kw)


def merged(*results, config=None):
    merger = TranscriptMerger(config)
    for result in results:
        merger.add(result)
    return merger.final()


# ---------------------------------------------------------------- text only (the plan's §100)

def test_exact_overlap_is_kept_once():
    m = merged(chunk(1, "I want to order a large pizza"), chunk(2, "large pizza for tonight"))
    assert m.text == "I want to order a large pizza for tonight"
    assert m.dedup == ["#2: removed 'large pizza' (exact)"]
    assert m.chunks == 2 and m.final


def test_case_differences():
    m = merged(chunk(1, "I want to order a Large Pizza"), chunk(2, "large pizza for tonight"))
    assert m.text == "I want to order a Large Pizza for tonight"  # the previous chunk's own casing stays
    assert m.dedup == ["#2: removed 'large pizza' (normalized)"]


def test_punctuation_and_apostrophe_differences():
    m = merged(chunk(1, "We need the server's logs, today"), chunk(2, "servers logs today, and the metrics"))
    assert m.text == "We need the server's logs, today and the metrics"


def test_a_period_that_only_came_from_the_cut_is_dropped():
    assert merged(chunk(1, "I want a large pizza."), chunk(2, "large pizza for tonight")).text == \
        "I want a large pizza for tonight"
    # The next chunk heard what followed: its comma replaces the period.
    assert merged(chunk(1, "I want a large pizza."), chunk(2, "large pizza, extra cheese")).text == \
        "I want a large pizza, extra cheese"


def test_a_real_sentence_end_stays():
    assert merged(chunk(1, "I want a large pizza."), chunk(2, "large pizza Tonight we eat")).text == \
        "I want a large pizza. Tonight we eat"
    assert merged(chunk(1, "I want a large pizza."), chunk(2, "Large pizza. Then we go home")).text == \
        "I want a large pizza. Then we go home"


def test_a_one_word_mismatch_is_not_a_duplicate():
    # Two different words in the overlap: maybe a mishearing, maybe the user correcting themselves. Both stay.
    m = merged(chunk(1, "I want to order a large pizza"), chunk(2, "a huge pizza for tonight"))
    assert m.text == "I want to order a large pizza a huge pizza for tonight"


def test_a_self_correction_to_a_similar_day_is_kept():
    # "Tuesday"/"Thursday" are 0.8 alike to difflib: days, months and numbers are never spelling variants.
    m = merged(chunk(1, "Meet me on Tuesday"), chunk(2, "on Thursday at noon"))
    assert m.text == "Meet me on Tuesday on Thursday at noon"


def test_fuzzy_mismatch():
    m = merged(chunk(1, "I want to order a large pizza"), chunk(2, "large piza for tonight"))
    assert m.text == "I want to order a large pizza for tonight"
    assert m.dedup == ["#2: removed 'large piza' (fuzzy)"]


def test_one_similar_word_alone_is_not_enough():
    m = merged(chunk(1, "I want a large pizza"), chunk(2, "piza for tonight"))
    assert m.text == "I want a large pizza piza for tonight"
    assert m.dedup == ["#2: kept both: weak match 'piza'"]


def test_a_negation_is_never_a_spelling_variant():
    # "should"/"shouldn't" are 0.86 alike: merging them would flip the meaning.
    m = merged(chunk(1, "I said you should go"), chunk(2, "you shouldn't go alone"))
    assert m.text == "I said you should go you shouldn't go alone"


def test_number_words_and_digits_are_the_same():
    m = merged(chunk(1, "I need five apples"), chunk(2, "5 apples please"))
    assert m.text == "I need five apples please"


def test_different_numbers_are_both_kept():
    m = merged(chunk(1, "the code is 1234567"), chunk(2, "code is 1234568 thanks"))
    assert "1234567" in m.text and "1234568" in m.text
    assert m.text.count("code is") == 1


def test_no_overlap_appends():
    m = merged(chunk(1, "hello world"), chunk(2, "hello world again", start=20.0, overlap_end=20.0))
    assert m.text == "hello world hello world again"
    assert m.dedup == []


@pytest.mark.parametrize("word", ["the", "a", "and", "I"])
def test_false_overlap_of_a_common_word_keeps_both(word):
    m = merged(chunk(1, f"this is what {word}"), chunk(2, f"{word} said yesterday"))
    assert m.text == f"this is what {word} {word} said yesterday"
    assert m.dedup == [f"#2: kept both: weak match '{word}'"]


def test_one_long_uncommon_word_is_enough():
    m = merged(chunk(1, "we moved everything to Kubernetes"), chunk(2, "Kubernetes last week"))
    assert m.text == "we moved everything to Kubernetes last week"


# ---------------------------------------------------------------- partial words at a forced cut (§97)

def test_a_word_cut_at_the_end_of_the_previous_chunk():
    m = merged(chunk(1, "I deployed the Post"), chunk(2, "PostgreSQL service."))
    assert m.text == "I deployed the PostgreSQL service."  # never "Post PostgreSQL", never "Post"
    assert m.dedup == ["#2: removed 'Post' (partial word)"]


def test_a_word_cut_at_the_start_of_the_next_chunk():
    m = merged(chunk(1, "we finished the deployment"), chunk(2, "ployment was done"))
    assert m.text == "we finished the deployment was done"


def test_a_cut_word_after_a_repeated_word():
    m = merged(chunk(1, "I deployed the Post"), chunk(2, "the PostgreSQL service."))
    assert m.text == "I deployed the PostgreSQL service."
    assert m.dedup == ["#2: removed 'the Post' (normalized)"]


@pytest.mark.parametrize("prev, new, expected", [
    ("I like the", "theory of it", "I like the theory of it"),  # "the" is a word, not the start of "theory"
    ("I ate a", "apple today", "I ate a apple today"),
    ("I gave an", "answer", "I gave an answer"),
    ("I parked the car", "carpet cleaning", "I parked the car carpet cleaning"),  # a short piece proves nothing
])
def test_a_short_or_common_piece_is_not_a_cut_word(prev, new, expected):
    assert merged(chunk(1, prev), chunk(2, new)).text == expected


def test_a_garbled_first_word_of_the_next_chunk():
    m = merged(chunk(1, "I want to order a large pizza"), chunk(2, "rder a large pizza for tonight"))
    assert m.text == "I want to order a large pizza for tonight"


def test_a_word_only_the_next_chunk_heard_is_kept():
    m = merged(chunk(1, "I want to order a large pizza"), chunk(2, "very large pizza for tonight"))
    assert m.text == "I want to order a very large pizza for tonight"
    assert m.dedup == ["#2: removed 'large pizza' (exact), added 'very'"]


def test_a_garbled_last_word_of_the_previous_chunk():
    m = merged(chunk(1, "I want a large pizza uh"), chunk(2, "large pizza for tonight"))
    assert m.text == "I want a large pizza uh for tonight"


# ---------------------------------------------------------------- timestamps

PIZZA = [("I", 17.0, 17.2), ("want", 17.3, 17.5), ("to", 17.6, 17.7), ("order", 17.8, 18.2), ("a", 18.4, 18.5),
         ("large", 18.9, 19.3), ("pizza", 19.4, 19.9)]


def test_timestamps_find_the_duplicates():
    m = merged(timed(1, PIZZA), timed(2, [("large", 19.0, 19.3), ("pizza", 19.42, 19.88), ("for", 20.0, 20.2),
                                          ("tonight", 20.3, 20.8)]))
    assert m.text == "I want to order a large pizza for tonight"
    assert m.dedup == ["#2: removed 'large pizza' (timestamps)"]
    assert [(w.text, w.start) for w in m.words][-4:] == [("large", 18.9), ("pizza", 19.4), ("for", 20.0), ("tonight", 20.3)]


def test_timestamps_with_a_slightly_different_text():
    m = merged(timed(1, PIZZA), timed(2, [("Large", 19.0, 19.3), ("piza,", 19.45, 19.9), ("for", 20.0, 20.2),
                                          ("tonight", 20.3, 20.8)]))
    assert m.text == "I want to order a large pizza for tonight"


def test_timestamps_say_a_repeated_phrase_is_not_a_duplicate():
    first = timed(1, [("he", 16.4, 16.6), ("said", 16.6, 16.9), ("very", 17.0, 17.3), ("good", 17.4, 17.8)])
    second = timed(2, [("very", 20.2, 20.5), ("good", 20.6, 20.9), ("indeed", 21.0, 21.5)])
    assert merged(first, second).text == "he said very good very good indeed"  # the user said it twice
    # Without the times, the same text would look like an overlap.
    assert merged(chunk(1, first.text), chunk(2, second.text)).text == "he said very good indeed"


def test_the_user_really_said_very_very():
    first = timed(1, [("it", 18.0, 18.2), ("was", 18.3, 18.5), ("very", 18.6, 18.9)])
    second = timed(2, [("very", 19.3, 19.6), ("good", 19.7, 20.1)])
    m = merged(first, second)
    assert m.text == "it was very very good"
    assert m.dedup == ["#2: kept both: 'very' said again (timestamps)"]


def test_a_stutter_in_the_overlap_is_kept_once_per_word():
    first = timed(1, [("it", 18.6, 18.8), ("was", 18.85, 19.1), ("very", 19.2, 19.5), ("very", 19.6, 19.9)])
    second = timed(2, [("very", 19.2, 19.5), ("very", 19.6, 19.9), ("good", 20.0, 20.4)])
    m = merged(first, second)
    assert m.text == "it was very very good"
    assert m.dedup == ["#2: removed 'very very' (timestamps)"]


def test_timestamps_add_a_word_the_previous_chunk_missed():
    first = timed(1, [("I", 18.5, 18.6), ("want", 18.9, 19.2), ("go", 19.6, 19.8)])
    second = timed(2, [("want", 19.0, 19.2), ("to", 19.3, 19.4), ("go", 19.6, 19.8), ("home", 20.0, 20.3)])
    m = merged(first, second)
    assert m.text == "I want to go home"
    assert [w.text for w in m.words] == ["I", "want", "to", "go", "home"]
    assert m.dedup == ["#2: removed 'want go' (timestamps), added 'to'"]


def test_timestamps_drop_another_transcription_of_the_same_moment():
    first = timed(1, [("a", 18.75, 18.85), ("large", 18.9, 19.3), ("pepperoni", 19.35, 19.75), ("pizza", 19.8, 20.0)])
    second = timed(2, [("large", 19.0, 19.3), ("macaroni", 19.35, 19.75), ("pizza", 19.8, 20.1), ("tonight", 20.2, 20.6)])
    m = merged(first, second)
    assert m.text == "a large pepperoni pizza tonight"
    assert m.dedup == ["#2: removed 'large macaroni pizza' (timestamps)"]


def test_timestamps_keep_both_numbers_when_they_disagree():
    first = timed(1, [("room", 18.9, 19.2), ("fifteen", 19.3, 19.6), ("please", 19.7, 20.0)])
    second = timed(2, [("room", 19.0, 19.2), ("fifty", 19.3, 19.6), ("please", 19.7, 20.0), ("thanks", 20.2, 20.5)])
    assert merged(first, second).text == "room fifteen fifty please thanks"


def test_timestamps_and_a_word_cut_at_the_end_of_the_previous_chunk():
    first = timed(1, [("I", 18.0, 18.1), ("deployed", 18.2, 18.8), ("the", 19.3, 19.5), ("Post", 19.6, 20.0)])
    second = timed(2, [("the", 19.3, 19.5), ("PostgreSQL", 19.6, 20.4), ("service.", 20.5, 21.0)])
    m = merged(first, second)
    assert m.text == "I deployed the PostgreSQL service."
    assert [w.text for w in m.words] == ["I", "deployed", "the", "PostgreSQL", "service."]
    assert m.dedup == ["#2: removed 'the Post' (timestamps)"]


def test_timestamps_and_a_word_cut_at_the_start_of_the_next_chunk():
    first = timed(1, [("we", 18.0, 18.1), ("finished", 18.1, 18.35), ("the", 18.4, 18.6), ("deployment", 18.7, 19.3)])
    second = timed(2, [("ployment", 19.0, 19.3), ("was", 19.5, 19.7), ("done", 19.8, 20.2)])
    assert merged(first, second).text == "we finished the deployment was done"


def test_a_timed_side_without_speech_in_the_overlap_means_a_pause():
    first = timed(1, [("a", 16.0, 16.1), ("large", 16.2, 16.6), ("pizza", 16.7, 17.0)])
    assert merged(first, chunk(2, "large pizza for tonight")).text == "a large pizza large pizza for tonight"


@pytest.mark.parametrize("timed_first", [True, False])
def test_mixed_timed_and_untimed_chunks(timed_first):
    head = [("large", 19.0, 19.3), ("pizza", 19.42, 19.88), ("for", 20.0, 20.2), ("tonight", 20.3, 20.8)]
    first = timed(1, PIZZA) if timed_first else chunk(1, "I want to order a large pizza")
    second = chunk(2, "large pizza for tonight") if timed_first else timed(2, head)
    m = merged(first, second)
    assert m.text == "I want to order a large pizza for tonight"
    assert len(m.words) == (7 if timed_first else 2)  # word times where a chunk had them


def test_chunk_relative_word_times_count_as_untimed():
    second = timed(2, [("large", 0.0, 0.3), ("pizza", 0.42, 0.88), ("for", 1.0, 1.2), ("tonight", 1.3, 1.8)])
    assert all(t.start is None for t in tokenize(second))
    assert merged(timed(1, PIZZA), second).text == "I want to order a large pizza for tonight"


def test_words_are_matched_to_the_text_s_tokens():
    result = chunk(1, "I love New York.", [WordInfo("I", 0.0, 0.1), WordInfo("love", 0.2, 0.5),
                                            WordInfo("New York", 0.6, 1.2)])
    tokens = tokenize(result)
    assert [t.text for t in tokens] == ["I", "love", "New", "York."]
    assert [w.text for w in tokens[2].words] == ["New York"] and tokens[3].words == []
    assert tokens[3].start == tokens[2].end == 1.2  # a token without its own word sits after the previous one


# ---------------------------------------------------------------- a whole session

def test_three_chunks_progressively_then_final():
    merger = TranscriptMerger(ChunkingConfig())
    one = merger.add(chunk(1, "the quick brown fox jumps"))
    assert (one.text, one.chunks, one.final) == ("the quick brown fox jumps", 1, False)
    two = merger.add(chunk(2, "fox jumps over the lazy"))
    assert (two.text, two.chunks, two.final) == ("the quick brown fox jumps over the lazy", 2, False)
    three = merger.add(chunk(3, "the lazy dog sleeps"))
    assert three.text == "the quick brown fox jumps over the lazy dog sleeps" and not three.final
    final = merger.final()
    assert (final.text, final.chunks, final.final) == (three.text, 3, True)
    assert final.dedup == ["#2: removed 'fox jumps' (exact)", "#3: removed 'the lazy' (exact)"]


def test_empty_chunks_change_nothing():
    merger = TranscriptMerger()
    merger.add(chunk(1, "hello there"))
    assert merger.add(chunk(2, "")).text == "hello there"
    assert merger.add(chunk(3, "   ")).text == "hello there"
    # Chunk 4's overlap is chunk 3's (empty) audio: chunk 1's words are too far back to be repeated.
    final = merger.add(chunk(4, "hello there friend"))
    assert final.text == "hello there hello there friend" and final.chunks == 4


def test_a_chunk_entirely_inside_the_overlap_adds_nothing():
    m = merged(chunk(1, "I want to order a large pizza"), chunk(2, "large pizza", end=20.4))
    assert m.text == "I want to order a large pizza"
    assert m.dedup == ["#2: removed 'large pizza' (exact)"]
    m = merged(timed(1, PIZZA), timed(2, [("large", 19.0, 19.3), ("pizza", 19.42, 19.88)], end=20.4))
    assert m.text == "I want to order a large pizza" and len(m.words) == 7


def test_the_configured_overlap_bounds_the_text_window():
    first = chunk(1, "alpha bravo charlie delta echo foxtrot golf hotel")
    second = chunk(2, "bravo charlie delta echo foxtrot golf hotel india")
    assert merged(first, second).text == "alpha bravo charlie delta echo foxtrot golf hotel india"
    # A quarter-second overlap can't hold seven words: they are not looked for.
    assert merged(first, second, config=ChunkingConfig(overlap_ms=250)).text == f"{first.text} {second.text}"


def test_results_must_come_in_order_and_final_is_final():
    merger = TranscriptMerger()
    merger.add(chunk(2, "hello"))
    with pytest.raises(ValueError):
        merger.add(chunk(1, "world"))
    merger.final()
    with pytest.raises(RuntimeError):
        merger.add(chunk(3, "again"))


def test_merging_is_deterministic():
    results = [chunk(1, "I want to order a large pizza"), chunk(2, "large piza for tonight"), chunk(3, "tonight please")]
    assert merged(*results) == merged(*results)


def test_merge_boundary_alone():
    tail = [Token(t) for t in "I want a large pizza".split()]
    head = [Token(t) for t in "large pizza for tonight".split()]
    joined = merge_boundary(tail, head, 19.0, 20.0)
    assert (joined.removed, joined.level) == (["large", "pizza"], "exact")
    assert [t.text for t in joined.tail + joined.head] == "I want a large pizza for tonight".split()
    assert merge_boundary(tail, head, 19.0, 19.0).head == head  # no overlap: nothing to look at


@pytest.mark.parametrize("token, expected", [("Five,", "5"), ("don't", "dont"), ("twenty-five", "25"), ("Ｐｉｚｚａ", "pizza"),
                                             ("PostgreSQL.", "postgresql"), ("—", ""), ("பீட்சா", "பீட்சா")])
def test_normalize(token, expected):
    assert normalize(token) == expected


# ---------------------------------------------------------------- other scripts and speed

def test_tamil():
    m = merged(chunk(1, "நான் ஒரு பெரிய பீட்சா ஆர்டர் செய்ய விரும்புகிறேன்"), chunk(2, "செய்ய விரும்புகிறேன் இன்று இரவு"))
    assert m.text == "நான் ஒரு பெரிய பீட்சா ஆர்டர் செய்ய விரும்புகிறேன் இன்று இரவு"


def test_japanese_does_not_crash():
    m = merged(chunk(1, "大きなピザを注文したいです"), chunk(2, "注文したいです。今夜のために"))
    assert m.text.startswith("大きなピザを注文したいです") and m.text.endswith("今夜のために")
    words = [("今夜", 19.2, 19.5), ("の", 19.5, 19.6), ("ため", 19.6, 19.9), ("に", 19.9, 20.0)]
    second = chunk(2, "今夜のために", [WordInfo(w, s, e) for w, s, e in words])
    m = merged(timed(1, [("大きな", 18.0, 18.4), ("ピザ", 18.5, 18.9)]), second)
    assert m.text == "大きな ピザ 今夜のために" and len(m.words) == 6


VOCABULARY = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec "
              "romeo sierra tango uniform victor whiskey xray yankee zulu").split()


@pytest.mark.parametrize("with_times", [False, True])
def test_two_hundred_chunks_merge_quickly(with_times):
    words = [VOCABULARY[n % len(VOCABULARY)] for n in range(200 * 46 + 4)]
    results = []
    for seq in range(1, 201):
        first = (seq - 1) * 46  # each chunk repeats the previous one's last 4 words (one second)
        start = first * 0.25
        spec = [(words[n], n * 0.25, n * 0.25 + 0.2) for n in range(first, first + 50)]
        overlap_end = start + (1.0 if seq > 1 else 0.0)
        result = (timed(seq, spec, start=start, end=spec[-1][2] + 0.05, overlap_end=overlap_end) if with_times else
                  chunk(seq, " ".join(w for w, _, _ in spec), start=start, end=spec[-1][2] + 0.05, overlap_end=overlap_end))
        results.append(result)
    fastest = float("inf")  # the fastest of three runs: other work on the machine slows one run, not all of them
    for _ in range(3):
        began = time.perf_counter()
        m = merged(*results)
        fastest = min(fastest, time.perf_counter() - began)
    assert fastest < 1.0
    assert m.text == " ".join(words) and m.chunks == 200
