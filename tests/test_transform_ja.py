"""Phase 40's transforms: Fix grammar, the Japanese Teams message and emails, and Translate in place, and the guard's
Japanese and cross-language readers. The research's before/after examples (research_notes "Rflow Text Transform
expansion", 1.6) are the acceptance tests; the model is always a fake."""
import pytest

from sst.settings import Settings
from sst.transform import (
    TRANSFORMS,
    Context,
    Transformer,
    TransformGuard,
    _values,
    menu_items,
    render,
    split_key,
    system_prompt,
    target_language,
    translate_key,
)

TANAKA = "田中さん、明日の会議なんだけど、資料まだできてなくて、たぶん15時ぐらいになると思う、ごめん"
TANAKA_TEAMS = ("田中さん\nお疲れ様です。\n明日の会議資料ですが、まだ完成しておらず、15時頃になる見込みです。\n"
                "遅くなり申し訳ありません。\n"
                "よろしくお願いいたします。")
SUZUKI = ("hi suzuki san, i finish the test for login page, two bug found, one is critical, i will fix by tomorrow morning, "
          "can you review PR after that")
SUZUKI_TEAMS = ("Suzukiさん\nお疲れ様です。\nログイン画面のテストが完了しました。\n"
                "バグが2件見つかり、うち1件はクリティカルです。\n"
                "明日の午前中までに修正しますので、その後PRのレビューをお願いできますか。\nよろしくお願いいたします。")
YAMAMOTO = "山本さん、見積もりの件ありがとうございます、ちょっと社内で確認するんで、来週の水曜までに返事します"
YAMAMOTO_EMAIL = ("山本様\n\nいつもお世話になっております。\nお見積もりの件、ありがとうございます。\n"
                  "社内で確認のうえ、来週水曜日までにご連絡"
                  "いたします。\n何卒よろしくお願いいたします。")
SATO = ("Sato-san quick question, the client want to move release to 10/15 but QA need maybe one more week, what should we "
        "do, option A keep 10/15 with less testing, option B 10/22")
SATO_TEAMS = ("Satoさん\nお疲れ様です。\nリリース日についてご相談です。\n"
              "クライアントから、リリースを10/15に変更したいとの要望がありました。\n"
              "ただ、QAにはもう1週間ほど必要になるかもしれません。\n・A案：10/15のまま、テストを縮小して対応\n・B案：10/22に変更\n"
              "どちらで進めるべきか、ご意見をいただけますか。\nよろしくお願いいたします。")
INVOICE = "Can you send me the invoice by Friday?"
MEETING = "来週の定例会議は木曜日の午後3時からに変更になりました。"


def check(original, result, transform, terms=(), context=None, target=""):
    return TransformGuard().validate(original, result, transform, terms, context, target)


# ---- the research's examples: accepted...

@pytest.mark.parametrize("original, result, transform", [
    (TANAKA, TANAKA_TEAMS, "teams"),  # 1.6 #1: たぶん…と思う as 見込み, ぐらい as 頃, ごめん as 申し訳ありません
    (SUZUKI, SUZUKI_TEAMS, "teams"),  # 1.6 #2: English into Japanese; Suzuki stays Suzuki
    (YAMAMOTO, YAMAMOTO_EMAIL, "email_external"),  # 1.6 #3: さん as 様 on the addressee line, 来週の水曜 stays relative
    (SATO, SATO_TEAMS, "teams"),  # 1.6 #4: both options, both dates, the maybe, the question
    (INVOICE, "金曜日までに請求書を送っていただけますか？", "translate"),  # section 6 #1
    (INVOICE, "金曜日までに請求書を送っていただけますか？", "translate:Japanese"),
    (MEETING, "Next week's regular meeting has moved to Thursday at 3 PM.", "translate:English"),  # 午後3時 = 3 PM
    ("予算は2万5000円で、締め切りは１０月１５日です。", "The budget is 25,000 yen, and the deadline is October 15.", "translate"),
    ("バグが二つあって、15時までに直せないかもしれない", "There are two bugs, and we might not be able to fix them by 3 PM.",
     "translate:English"),
    ("Can we meet at 3:30 PM on 10/15? If not, maybe next Monday.",
     "10/15の15時30分にお会いできますか？できない場合は、来週の月曜日になるかもしれません。", "translate:Japanese"),
    ("i finish the test for login page, two bug found, one is critical",
     "I finished the test for the login page; two bugs were found, and one is critical.", "grammar"),
    ("明日の会議わ10時からです。資料を確認しといてください。", "明日の会議は10時からです。資料を確認しておいてください。",
     "grammar"),
    ("明日の会議は、えっと、たぶん15時からになると思います。", "明日の会議は、たぶん15時からになると思います。", "concise"),
    ("来週、新しいサーバーに移行したいと思います。", "来週、新しいサーバーへ移行したいと思います。",
     "rewrite"),  # たいと思う: no hedge
])
def test_good_results_are_accepted(original, result, transform):
    got = check(original, result, transform)
    assert got.accepted, got.reasons


def test_the_writers_name_from_the_settings_is_allowed():
    email = YAMAMOTO_EMAIL.replace("おります。\n", "おります。\n株式会社ABCの山田です。\n", 1)
    assert check(YAMAMOTO, email, "email_external", context=Context("山田", "株式会社ABC")).accepted
    got = check(YAMAMOTO, email, "email_external")  # no name set: never made up
    assert not got.accepted and "added the name '山田'" in got.reasons and "added the company '株式会社ABC'" in got.reasons


# ---- ...and bad ones rejected, for the right reason

@pytest.mark.parametrize("original, result, transform, reason", [
    (SUZUKI, SUZUKI_TEAMS.replace("Suzuki", "鈴木"), "teams",
     "wrote 'suzuki' as '鈴木' (names stay as written: no kanji is guessed)"),
    (TANAKA, TANAKA_TEAMS.replace("田中さん", "田中様"), "teams", "changed '田中さん' to '田中様'"),  # 様 only for external email
    (YAMAMOTO, YAMAMOTO_EMAIL.replace("山本様", "山本"), "email_external", "changed '山本さん' to '山本'"),
    (TANAKA, TANAKA_TEAMS.replace("15時", "16時"), "teams", "lost '15時'"),
    (TANAKA, TANAKA_TEAMS.replace("15時頃になる見込みです", "15時頃になります"), "teams", "dropped the uncertainty ('たぶん')"),
    (TANAKA, TANAKA_TEAMS.replace("まだ完成しておらず", "完成しており"), "teams", "dropped a negation ('なく')"),
    (TANAKA, TANAKA_TEAMS.replace("15時頃", "15時"), "teams", "dropped the qualifier ('ぐらい')"),
    (TANAKA[:-4], TANAKA_TEAMS, "teams", "added an apology ('申し訳')"),  # without ごめん, no apology may appear
    (TANAKA, TANAKA_TEAMS.replace("お疲れ様です。", "お疲れ様です。\nいつもお世話になっております。"), "teams",
     "wrote お世話になっております (for clients, not colleagues)"),
    (TANAKA, TANAKA_TEAMS + "\n了解しました。", "teams", "wrote 了解しました (to a superior: 承知しました)"),
    (TANAKA, TANAKA_TEAMS.replace("お疲れ様です。", "ご苦労様です。"), "teams",
     "wrote ご苦労様 (rude to a superior: お疲れ様です)"),
    (TANAKA, TANAKA_TEAMS + "\nいつも助かります。", "teams", "added thanks ('助かり')"),
    (TANAKA, TANAKA_TEAMS + "\n必ず間に合わせます。", "teams", "added a promise ('必ず')"),
    ("明日の会議資料、15時頃になりそう、たぶん",
     "〇〇さん\nお疲れ様です。\n明日の会議資料は15時頃になる見込みです。\nよろしくお願いいたします。",
     "teams", "left a placeholder ('〇〇')"),
    ("明日の会議資料、15時頃になりそう、たぶん",
     "佐藤さん\nお疲れ様です。\n明日の会議資料は15時頃になる見込みです。\nよろしくお願いいたします。",
     "teams", "added the name '佐藤'"),
    (SUZUKI, SUZUKI_TEAMS.replace("お願いできますか。", "お願いします。"), "teams", "answered or dropped the question"),
    (SUZUKI, SUZUKI_TEAMS.replace("PR", "プルリクエスト"), "teams", "lost 'PR'"),
    (SATO, SATO_TEAMS.replace("かもしれません", "です"), "teams", "dropped the uncertainty ('maybe')"),
    (SATO, SATO_TEAMS.replace("\n・B案：10/22に変更", ""), "teams", "lost '10/22'"),
    (YAMAMOTO, "株式会社山本商事\n" + YAMAMOTO_EMAIL, "email_external", "added the company '株式会社山本商事'"),
    (YAMAMOTO, YAMAMOTO_EMAIL.replace("来週水曜日", "10月14日(水)"), "email_external", "lost '来週'"),
    (INVOICE, "はい、金曜日までに請求書をお送りします。", "translate", "answered or dropped the question"),
    (INVOICE, "Can you send me the invoice by Friday?", "translate:Japanese", "isn't in Japanese"),
    (INVOICE, "金曜日までに請求書を送っていただけますか？よろしくお願いします。", "translate", "added a sign-off ('よろしく')"),
    (INVOICE, "10/17(金)までに請求書を送っていただけますか？", "translate", "added '10/17'"),
    (MEETING, "Next week's regular meeting has moved to Thursday at 4 PM.", "translate", "lost '午後3時'"),
    (MEETING, "The regular meeting has moved to Thursday at 3 PM.", "translate", "lost '来週'"),
    ("予算は2万5000円です。", "The budget is 250,000 yen.", "translate", "lost '25000'"),
    ("田中さんに確認してください。", "Please check with Tanaka-san.", "translate", "dropped the name '田中'"),
    # (no guessed romaji)
    ("明日は行けません。", "I can go tomorrow.", "translate", "dropped a negation ('ません')"),
    ("i finish the test, two bug found", "The testing work has been completed successfully, and two defects were identified.",
     "grammar", "reworded more than a grammar fix (added 'work', 'completed', 'successfully')"),
    ("明日の会議わ10時からです。", "明日のミーティングの開始時刻は午前10時を予定しております。", "grammar",
     "changed more than a grammar fix"),
    ("明日の会議は、たぶん15時からになると思います。", "明日の会議は15時からです。", "concise",
     "dropped the uncertainty ('たぶん')"),
    ("明日の会議は、たぶん15時からになると思います。", "The meeting tomorrow will probably start at 3 PM.", "concise",
     "translated the text"),
])
def test_bad_results_are_rejected_for_the_right_reason(original, result, transform, reason):
    got = check(original, result, transform)
    assert not got.accepted and reason in got.reasons, got.reasons


# ---- reading values in either language

@pytest.mark.parametrize("text, values", [
    ("2万5000円", {"n:25000"}), ("2.5万", {"n:25000"}), ("1億2000万", {"n:120000000"}), ("１５個", {"n:15"}),
    ("25,000 dollars", {"n:25000"}), ("二十五件", {"n:25"}), ("十分です", set()),
    ("10/15", {"d:10/15"}), ("10月15日", {"d:10/15"}), ("October 15th", {"d:10/15"}), ("15 Oct", {"d:10/15"}),
    ("15時", {"t:15:00", "n:15"}), ("午後3時", {"t:15:00"}), ("3 PM", {"t:15:00"}), ("3時半", {"t:3:30", "t:15:30", }),
    ("Friday", {"w:4"}), ("金曜日", {"w:4"}), ("(水)", {"w:2"}),
    ("明日", {"r:tomorrow"}), ("明後日", {"r:dayafter"}), ("next Monday", {"r:nextweek", "w:0"}), ("来週", {"r:nextweek"}),
    ("a@b.com", {"e:a@b.com"}), ("GPT-4o", {"c:GPT-4o"}),
])
def test_values_read_the_same_in_both_languages(text, values):
    found, _ = _values(text)
    assert set().union(*(alts for _, alts in found)) == values


def test_one_may_go_or_come():
    found, maybe = _values("one more week")
    assert found == [] and maybe == {"n:1"}


# ---- prompts

def test_translate_lifts_never_translate_and_names_the_language_and_register():
    prompt = system_prompt("translate:English:formal", target="English")
    assert "never translate" not in prompt and "Write in English: translate the whole text into natural English" in prompt
    assert "Use a formal, polite register." in prompt and "never turned into kanji" in prompt and "15時" in prompt
    assert "Keep its tone" in system_prompt("translate", target="Japanese")
    assert "greetings or sign-offs" in prompt  # nothing added either


def test_the_japanese_templates():
    teams = system_prompt("teams")
    assert "if the text is in another language, translate it into Japanese" in teams and "never translate" not in teams
    for rule in ("お疲れ様です。", "結論から", "よろしくお願いいたします。", "no お世話になっております", "no 了解しました",
                 "never invent a name", "apologies, thanks or promises", "fixed phrases of the template", "・"):
        assert rule in teams, rule
    external = system_prompt("email_external", context=Context("山田", "株式会社ABC"))
    assert "いつもお世話になっております。\n株式会社ABCの山田です。" in external and "様" in external
    assert "お疲れ様です。\n山田です。" in system_prompt("email_internal", context=Context("山田", "株式会社ABC"))
    nameless = system_prompt("email_external")
    assert "No line naming the writer" in nameless and "株式会社ABC" not in nameless


def test_fix_grammar_changes_only_the_errors():
    prompt = system_prompt("grammar")
    assert "Fix only spelling, grammar, punctuation" in prompt and "never translate" in prompt
    assert TRANSFORMS["grammar"].minimal and TRANSFORMS["grammar"].group == "Tone"


def test_translate_keys():
    assert split_key("translate:Japanese:formal") == ("translate", "Japanese", "formal")
    assert translate_key("English", "casual") == "translate:English:casual" and translate_key() == "translate"
    assert TRANSFORMS["translate:Japanese"].name == "Translate to Japanese"
    assert TRANSFORMS["translate:English:formal"].name == "Translate to English, formally"
    assert TRANSFORMS["translate:Japanese"].instruction == TRANSFORMS["translate"].instruction
    for bad in ("translate:Klingon", "translate:English:shouty", "concise:English", "poem"):
        with pytest.raises(KeyError):
            TRANSFORMS[bad]
    assert "translate:Japanese" not in TRANSFORMS  # looked up, never listed


def test_the_target_without_a_language_named_is_translates_own():
    context = Context(translate_to="Japanese", translate_second="English")
    assert target_language("translate", INVOICE, context) == "Japanese"
    assert target_language("translate", MEETING, context) == "English"  # already Japanese: the second language
    assert target_language("translate:Tamil", MEETING, context) == "Tamil"
    assert target_language("teams", INVOICE, context) == "Japanese" and target_language("concise", INVOICE) == ""


def test_the_context_comes_from_the_settings():
    s = Settings(signature_name=" 山田  太郎 ", signature_company="株式会社ABC", translate_to="Japanese")
    assert Context.of(s, "Japanese") == Context("山田 太郎", "株式会社ABC", "Japanese", "English")
    assert Context.of(Settings(translate_second="Tamil")).translate_second == "Tamil"


# ---- the transformer with a fake model

class FakeModel:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, prompt, text):
        self.calls.append((prompt, text))
        return self.answers.pop(0)


def test_translate_in_place_into_the_chosen_language():
    model = FakeModel("金曜日までに請求書を送っていただけますか？")
    got = Transformer(model, context=Context(translate_to="Japanese", translate_second="English")).transform(INVOICE,
                                                                                                             "translate")
    assert got.accepted and got.transform == "translate:Japanese" and TRANSFORMS[got.transform].name == "Translate to Japanese"
    assert "Write in Japanese" in model.calls[0][0] and got.plain == "金曜日までに請求書を送っていただけますか？"


def test_a_named_language_and_register_reach_the_prompt():
    model = FakeModel("Next week's regular meeting has moved to Thursday at 3 PM.")
    got = Transformer(model).transform(MEETING, "translate:English:formal")
    assert got.accepted and got.transform == "translate:English:formal"
    assert "Write in English" in model.calls[0][0] and "formal, polite register" in model.calls[0][0]


def test_a_guessed_kanji_name_gets_one_repair_then_the_text_stays():
    model = FakeModel(SUZUKI_TEAMS.replace("Suzuki", "鈴木"), SUZUKI_TEAMS)
    got = Transformer(model).transform(SUZUKI, "teams")
    assert got.accepted and got.attempts == 2 and "no kanji is guessed" in model.calls[1][0]
    model = FakeModel(*[SUZUKI_TEAMS.replace("Suzuki", "鈴木")] * 2)
    got = Transformer(model).transform(SUZUKI, "teams")
    assert not got.accepted and got.plain == "" and got.attempts == 2


def test_the_email_signature_reaches_the_prompt_and_the_check():
    email = YAMAMOTO_EMAIL.replace("おります。\n", "おります。\n株式会社ABCの山田です。\n", 1)
    model = FakeModel(email)
    got = Transformer(model, context=Context("山田", "株式会社ABC")).transform(YAMAMOTO, "email_external")
    assert got.accepted and "株式会社ABCの山田です。" in model.calls[0][0]


def test_japanese_bullets_stay_japanese():
    plain, markup = render("佐藤さん\n・A案：10/15のまま\n・B案：10/22に変更\n- other")
    assert plain == "佐藤さん\n・A案：10/15のまま\n・B案：10/22に変更\n- other"
    assert markup == "<p>佐藤さん</p><p>・A案：10/15のまま</p><p>・B案：10/22に変更</p><ul><li>other</li></ul>"


def test_menu_items_are_grouped_and_translate_keeps_its_letter():
    chosen = ["translate", "teams", "concise", "bullets", "grammar", "professional", "actions", "rewrite", "email_internal",
              "email_external"]
    items = menu_items(chosen)
    assert [hint for _, hint, _ in items] == ["1", "2", "3", "4", "5", "6", "7", "8", "9", "T"]
    assert [key for key, _, _ in items] == ["concise", "grammar", "professional", "rewrite", "teams", "bullets", "actions",
                                            "email_internal", "email_external", "translate"]
    assert menu_items(["actions", "nope", "concise"]) == [("concise", "1", "Concise"), ("actions", "2", "Action items")]
