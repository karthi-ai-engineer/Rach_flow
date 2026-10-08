"""The final guard (the plan's §61-68): may the LLM's cleanup be typed, or the trusted formatted text instead?

The LLM never grades its own work (§58): the guard compares the formatted text (`before`) with the LLM's (`after`) on its
own, deterministically, and any doubt rejects, because the fallback, the formatted text, is always safe to type (§68).
  - Protected values (extract_entities: numbers, dates, times, money, emails, URLs, the user's terms...) come back as they
    were: none lost, none new ("5:30" -> "6:30" is one of each). A number may change between words and digits ("five", "5").
  - Meaning stays: negations, the speech act (a question stays a question, a command a command, §57), uncertainty
    ("maybe"), names; no word is swapped for another ("increase" -> "decrease", "on" -> "off") and nothing is added (§67).
  - Words may go only where cleanup is expected to remove them (§65): fillers ("um", "you know", "some kind of", "like"
    where it isn't a verb or a comparison, "so", "yeah", "okay" opening a clause, "yeah" closing one), stutters and a word
    said again and again ("project project project"), a false start abandoned for a restart, and the words a clear
    self-correction replaced, when its replacement made it into `after` (§56: "send this tomorrow no wait Friday" -> "send
    this Friday"). Anything else: at most one dropped word. A filler may go, but not turn into a new word ("yeah" -> "PC").
The change ratio is recorded and makes the word counts stricter when it is high, but never rejects alone: a dictation full
of fillers legitimately changes a lot (§66). Scripts written without spaces (Japanese) are split where the script changes,
so they are compared roughly, but nothing crashes on them and a translation is never let through.
"""
import bisect
import difflib
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sst.pipeline.contracts import GuardConfig, GuardResult, ProtectedEntity

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- protected entities

_SMALL = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                     "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_SCALES = {"thousand": 10**3, "million": 10**6, "billion": 10**9, "trillion": 10**12}
_ORDINALS = {w: i for i, w in enumerate("first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
                                        "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth "
                                        "twentieth".split(), 1)}
_ORDINALS |= {f"twenty {w}": 20 + i for w, i in list(_ORDINALS.items())[:9]} | {"thirtieth": 30, "thirty first": 31}
_MONTHS = {m: i for i, m in enumerate("january february march april may june july august september october november "
                                      "december".split(), 1)}
_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
               "nov": 11, "dec": 12}
_WEEKDAYS = "monday tuesday wednesday thursday friday saturday sunday".split()
_ONE_DETERMINERS = {"the", "this", "that", "which", "every", "any", "some", "each", "no", "another", "a", "an", "other", "what"}


def _alt(words: Iterable[str]) -> str:
    """A regex alternation, longest first ("sept" before "sep"); a space also matches a hyphen ("twenty-first")."""
    return "|".join(re.escape(w).replace(r"\ ", r"[\s-]+") for w in sorted(words, key=len, reverse=True))


_MONTH = _alt([*_MONTHS, *_MONTH_ABBR])
_DAY = rf"(?:(?P<d>\d{{1,2}})(?:st|nd|rd|th)?|(?P<dw>{_alt(_ORDINALS)}))"
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_URL = re.compile(r"(?<![\w@.-])(?:[a-z][a-z0-9+.-]*://[^\s<>\"]+|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}"
                  r"(?![\w@-])(?::\d+)?(?:/[^\s<>\"]*)?)", re.I)
_MERIDIEM = r"(?P<mer>[ap])\.?\s?m\b\.?"
_TIME_HM = re.compile(rf"(?<![\w:.])(?P<h>[01]?\d|2[0-3]):(?P<mi>[0-5]\d)(?::(?P<s>[0-5]\d))?(?:\s?{_MERIDIEM})?(?![\w:])",
                      re.I)
_TIME_H = re.compile(rf"(?<![\w:.$])(?P<h>1[0-2]|0?[1-9])\s?{_MERIDIEM}(?!\w)", re.I)
_TIME_OCLOCK = re.compile(r"(?<![\w:.])(?P<h>1[0-2]|0?[1-9])\s+o['’]clock\b", re.I)
_TIME_WORD = re.compile(r"\b(?:noon|midday|midnight)\b", re.I)
_DATE_ISO = re.compile(r"(?<![\w-])(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?![\w-])")
_DATE_SLASH = re.compile(r"(?<![\w/])(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{4}|\d{2})(?![\w/])")
_DATE_MD = re.compile(rf"\b(?P<m>{_MONTH})\.?\s+(?:the\s+)?{_DAY}(?![\w:])(?:,?\s+(?P<y>\d{{4}})(?!\d))?", re.I)
_DATE_DM = re.compile(rf"\b(?:the\s+)?{_DAY}\s+(?:of\s+)?(?P<m>{_MONTH})\b\.?(?:,?\s+(?P<y>\d{{4}})(?!\d))?", re.I)
_DATE_MY = re.compile(rf"\b(?P<m>{_MONTH})\.?,?\s+(?P<y>\d{{4}})(?!\d)", re.I)
_MONTH_ONLY = re.compile(rf"\b(?P<m>{_alt(_MONTHS)})\b", re.I)
# "this Friday" is just Friday (and "send this Friday" sends "this"), but "next Friday" and "last Friday" are other days.
_RELDATE = re.compile(r"\b(?:(?:the\s+)?day\s+(?:after\s+tomorrow|before\s+yesterday)|today|tonight|tomorrow|yesterday|"
                      r"(?:this|next|last|coming|past)\s+(?:week|weekend|month|year|quarter|morning|afternoon|evening|night)|"
                      rf"(?:next|last|coming|past)\s+(?:{_alt(_WEEKDAYS)}|{_alt(_MONTHS)}))\b", re.I)
_WEEKDAY = re.compile(rf"\b(?P<w>{_alt(_WEEKDAYS)})s?\b", re.I)

# Amounts: a number in digits or words, then what it counts. Single-letter units only written onto the number ("5m").
_NUM = re.compile(r"(?<![\w.,/:-])(?P<n>\d{1,3}(?:,\d{3})+|\d+)(?P<f>\.\d+)?(?![.,]\d|[-/:]\w)")
_SCALE_AFTER = re.compile(rf"\s+({_alt(_SCALES)})\b", re.I)
_CURRENCY_BEFORE = re.compile(r"(?:(?P<sym>(?:US|A|C|S|HK|NZ)?\$|[€£¥₹₩₽₺₫₪₱₦฿])|\b(?P<code>USD|EUR|GBP|INR|JPY|[Rr]s\.?))\s?$")
_CURRENCY_CODES = {"USD": "$", "US$": "$", "EUR": "€", "GBP": "£", "INR": "₹", "RS": "₹", "JPY": "¥"}
_CURRENCY_SCALE = re.compile(r"(?P<s>k|K|mn|m|M|bn|B)\b")
_CURRENCY_SCALES = {"k": 10**3, "K": 10**3, "m": 10**6, "M": 10**6, "mn": 10**6, "bn": 10**9, "B": 10**9}
_CURRENCY_AFTER = re.compile(r"\s?(?P<code>USD|EUR|GBP|INR|JPY)\b|\s+(?P<word>(?i:dollars?|bucks?|euros?|pounds?|rupees?|yen|"
                             r"cents?))\b|\s?(?P<cent>¢)")
_CURRENCY_WORDS = {"dollar": "$", "buck": "$", "euro": "€", "pound": "£", "rupee": "₹", "yen": "¥", "cent": "¢"}
_PERCENT_AFTER = re.compile(r"\s?%|\s+(?i:percent|per\s+cent)\b")
_UNIT_ABBR = {"km": "km", "cm": "cm", "mm": "mm", "mi": "mi", "ft": "ft", "yd": "yd", "kg": "kg", "mg": "mg", "lb": "lb",
              "lbs": "lb", "oz": "oz", "ml": "ml", "kb": "KB", "mb": "MB", "gb": "GB", "tb": "TB", "pb": "PB", "kbps": "kbps",
              "mbps": "Mbps", "gbps": "Gbps", "hz": "Hz", "khz": "kHz", "mhz": "MHz", "ghz": "GHz", "kw": "kW", "kwh": "kWh",
              "mah": "mAh", "ms": "ms", "sec": "s", "secs": "s", "min": "min", "mins": "min", "hr": "h", "hrs": "h",
              "px": "px", "dpi": "dpi", "fps": "fps", "rpm": "rpm", "mph": "mph", "kph": "km/h", "km/h": "km/h", "°c": "°C",
              "°f": "°F", "°": "°"}
_UNIT_SINGLE = {"m": "m", "g": "g", "l": "l", "s": "s", "h": "h", "V": "V", "W": "W", "B": "B"}
_UNIT_WORDS = {w: unit for unit, words in {
    "km": "kilometer kilometers kilometre kilometres", "m": "meter meters metre metres",
    "cm": "centimeter centimeters centimetre centimetres", "mm": "millimeter millimeters millimetre millimetres",
    "mi": "mile miles", "ft": "foot feet", "in": "inch inches", "yd": "yard yards", "kg": "kilogram kilograms kilo kilos",
    "g": "gram grams", "mg": "milligram milligrams", "oz": "ounce ounces", "l": "liter liters litre litres",
    "ml": "milliliter milliliters millilitre millilitres", "B": "byte bytes", "KB": "kilobyte kilobytes",
    "MB": "megabyte megabytes", "GB": "gigabyte gigabytes", "TB": "terabyte terabytes", "Hz": "hertz", "kHz": "kilohertz",
    "MHz": "megahertz", "GHz": "gigahertz", "W": "watt watts", "kW": "kilowatt kilowatts", "V": "volt volts",
    "ms": "millisecond milliseconds", "s": "second seconds", "min": "minute minutes", "h": "hour hours", "day": "day days",
    "week": "week weeks", "month": "month months", "year": "year years", "°": "degree degrees"}.items() for w in words.split()}
_UNIT_AFTER = re.compile(rf"(?:\s?(?P<abbr>(?i:{_alt(_UNIT_ABBR)}))|(?P<single>[mglshVWB])|\s+(?P<word>(?i:{_alt(_UNIT_WORDS)})))"
                         r"(?![\w/])")
_ORDINAL_AFTER = re.compile(r"(st|nd|rd|th)\b", re.I)
_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?:[-._/][A-Za-z0-9]+)*")  # "H100", "GPT-4o", "v1.2.3", "COVID-19"
_NEGATION = re.compile(r"\b(?:not|never|no|nobody|nothing|none|nowhere|neither|nor|cannot|nope|nah|dunno)\b|(?<=[a-z])n['’]t\b",
                       re.I)


def extract_entities(text: str, terms: Iterable[str] = ()) -> list[ProtectedEntity]:
    """The values the LLM must keep, in written form (the formatter has already written "3:30 PM", "$25.50"...), sorted by
    position. `normalized` is what is compared, the same across written variants: times as 24 h "15:30", dates as ISO
    ("2026-10-01", "--10-01" without a year, "--10" a month alone), money as "$25.50", percentages "25%", units "5 km",
    numbers as plain decimals (digits or words: "twenty five" is "25"), emails and URLs in lower case without "https://" or
    "www.", identifiers with digits ("H100", "v1.2.3") as CODE, the user's `terms` (TERM) case-folded, and every negation
    (NEGATION: not, never, no, n't...) as "not". Values never overlap, except terms and negations, which are looked for
    in addition."""
    found: list[ProtectedEntity] = []
    taken: list[tuple[int, int]] = []

    def add(kind: str, start: int, end: int, normalized: str, claim: bool = True) -> None:
        if claim:
            if any(s < end and start < e for s, e in taken):
                return
            taken.append((start, end))
        found.append(ProtectedEntity(kind, text[start:end], normalized, (start, end)))

    for kind, pattern, read in _PATTERNS:
        for m in pattern.finditer(text):
            if got := read(m, text):
                add(kind, m.start(), *got)
    for kind, start, end, normalized in _amounts(text):
        add(kind, start, end, normalized)
    for m in _CODE.finditer(text):
        if any(c.isdigit() for c in m.group()):
            plain = re.fullmatch(r"\d+(?:\.\d+)?", m.group())
            add("NUMBER" if plain else "CODE", m.start(), m.end(), _dec(Decimal(m.group())) if plain else m.group().casefold())
    for term in _unique(terms):
        words = (re.sub("['’]", "['’]", re.escape(w)) for w in term.split())
        for m in re.finditer(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", text, re.I):  # either apostrophe
            add("TERM", m.start(), m.end(), " ".join(m.group().casefold().replace("’", "'").split()), claim=False)
    for m in _NEGATION.finditer(text):
        add("NEGATION", m.start(), m.end(), "not", claim=False)
    return sorted(found, key=lambda e: e.span)


def _unique(terms: Iterable[str]) -> list[str]:
    """The user's terms, each once (whatever its case), with single spaces."""
    seen: dict[str, str] = {}
    for term in terms:
        term = " ".join(str(term or "").split())
        if term:
            seen.setdefault(term.casefold(), term)
    return list(seen.values())


def _dec(value: Decimal) -> str:
    return format(value.normalize(), "f")  # "1,000" and "1000.0" -> "1000"; "3.50" -> "3.5"


def _money(value: Decimal) -> str:
    """Amounts keep their cents: "$25.5" and "$25.50" are one amount, "$25" and "$25.00" another."""
    if value != value.to_integral_value() and -value.normalize().as_tuple().exponent <= 2:
        return format(value.quantize(Decimal("0.01")), "f")
    return _dec(value)


def _month(word: str, text: str, at: int, alone: bool = False) -> int | None:
    """The month a word names, or None where it is more likely something else ("may", "march", "Jan" the name)."""
    low = word.lower()
    if low in _MONTH_ABBR:
        return _MONTH_ABBR[low] if word[0].isupper() and not alone else None
    if low in ("may", "march") and (word[0].islower() or alone and re.match(r"\s+(?:i|we|you|he|she|they|it|be|not)\b",
                                                                              text[at + len(word):], re.I)):
        return None
    return _MONTHS.get(low)


def _date(end: int, month: int | None, day: int | None = None, year: int | None = None) -> tuple[int, str] | None:
    if not month or not 1 <= month <= 12 or day is not None and not 1 <= day <= 31:
        return None
    text = f"{year:04d}-{month:02d}" if year is not None else f"--{month:02d}"
    return end, text + (f"-{day:02d}" if day is not None else "")


def _read_day_month(m: re.Match, text: str):
    day = int(m["d"]) if m["d"] else _ORDINALS[" ".join(re.split(r"[\s-]+", m["dw"].lower()))]
    return _date(m.end(), _month(m["m"], text, m.start("m")), day, int(m["y"]) if m["y"] else None)


def _read_slash(m: re.Match, text: str):
    a, b, year = int(m["a"]), int(m["b"]), int(m["y"]) + (2000 if len(m["y"]) == 2 else 0)
    return _date(m.end(), *((a, b) if a <= 12 else (b, a)), year)  # month first (US), unless it can't be a month


def _read_time(m: re.Match, text: str):
    g = m.groupdict()
    hour, minute, meridiem = int(g["h"]), int(g.get("mi") or 0), (g.get("mer") or "").lower()
    if meridiem == "p" and hour < 12:
        hour += 12
    elif meridiem == "a" and hour == 12:
        hour = 0
    return m.end(), f"{hour:02d}:{minute:02d}" + (f":{g['s']}" if g.get("s") else "")


def _read_url(m: re.Match, text: str):
    url = m.group().rstrip(".,;:!?)]}'\"")
    return m.start() + len(url), re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.lower()).removeprefix("www.").rstrip("/")


_PATTERNS = [  # in order of precedence: a value claims its span, so "October 1" is a date, not the number 1
    ("EMAIL", _EMAIL, lambda m, t: (m.end(), m.group().lower())),
    ("URL", _URL, _read_url),
    ("TIME", _TIME_HM, _read_time), ("TIME", _TIME_H, _read_time), ("TIME", _TIME_OCLOCK, _read_time),
    ("TIME", _TIME_WORD, lambda m, t: (m.end(), "00:00" if m.group().lower() == "midnight" else "12:00")),
    ("DATE", _DATE_ISO, lambda m, t: _date(m.end(), int(m["m"]), int(m["d"]), int(m["y"]))),
    ("DATE", _DATE_SLASH, _read_slash), ("DATE", _DATE_MD, _read_day_month), ("DATE", _DATE_DM, _read_day_month),
    ("DATE", _DATE_MY, lambda m, t: _date(m.end(), _month(m["m"], t, m.start("m")), None, int(m["y"]))),
    ("RELDATE", _RELDATE, lambda m, t: (m.end(), " ".join(m.group().lower().split()).removeprefix("the "))),
    ("DATE", _MONTH_ONLY, lambda m, t: _date(m.end(), _month(m["m"], t, m.start("m"), alone=True))),
    ("WEEKDAY", _WEEKDAY, lambda m, t: (m.end(), m["w"].lower())),
]


def _amounts(text: str):
    """Every number, in digits or in words, with what it counts: (kind, start, end, normalized)."""
    found = [(m.start(), m.end(), Decimal(m["n"].replace(",", "") + (m["f"] or "")), False) for m in _NUM.finditer(text)]
    found += [(start, end, value, True) for start, end, value in _word_numbers(text)]
    for start, end, value, spelled in sorted(found, key=lambda f: f[0]):
        if not spelled and (m := _SCALE_AFTER.match(text, end)):  # "2.5 million" (spelled scales are part of the words)
            value, end = value * _SCALES[m[1].lower()], m.end()
        if sign := _CURRENCY_BEFORE.search(text, max(0, start - 6), start):
            symbol = _CURRENCY_CODES.get((sign["sym"] or sign["code"]).upper().rstrip("."), sign["sym"] or "")
            if not spelled and (m := _CURRENCY_SCALE.match(text, end)):  # "$5k", "$2M"
                value, end = value * _CURRENCY_SCALES[m["s"]], m.end()
            end = m.end() if (m := _CURRENCY_AFTER.match(text, end)) else end  # "$5 dollars" is one amount
            yield "CURRENCY", sign.start(), end, symbol + _money(value)
        elif m := _CURRENCY_AFTER.match(text, end):
            symbol = _CURRENCY_CODES.get(m["code"] or "") or ("¢" if m["cent"] else
                                                              _CURRENCY_WORDS[m["word"].lower().rstrip("s")])
            if symbol == "¢":
                symbol, value = "$", value / 100
            yield "CURRENCY", start, m.end(), symbol + _money(value)
        elif m := _PERCENT_AFTER.match(text, end):
            yield "PERCENT", start, m.end(), _dec(value) + "%"
        elif m := _UNIT_AFTER.match(text, end):
            unit = (_UNIT_ABBR[m["abbr"].lower()] if m["abbr"] else _UNIT_SINGLE[m["single"]] if m["single"]
                    else _UNIT_WORDS[m["word"].lower()])
            yield "UNIT", start, m.end(), f"{_dec(value)} {unit}"
        elif not spelled and (m := _ORDINAL_AFTER.match(text, end)):
            yield "NUMBER", start, m.end(), _dec(value) + m[1].lower()
        elif not (end < len(text) and text[end].isalpha()):  # "3D", "4K", "10x" are identifiers: the CODE pass takes them
            yield "NUMBER", start, end, _dec(value)


def _word_numbers(text: str) -> list[tuple[int, int, Decimal]]:
    """Numbers written in words: (start, end, value). "one" after "the", "this", "no"... is a pronoun, not a number."""
    words = [(m.start(), m.end(), m.group().lower()) for m in re.finditer(r"[A-Za-z]+", text)]
    out, i = [], 0
    while i < len(words):
        j, value = _parse_number(words, i, text)
        pronoun = words[i][2] == "one" and j == i + 1 and (
            _ONE_DETERMINERS.intersection(w for _, _, w in words[max(0, i - 2):i])
            or i + 1 < len(words) and words[i + 1][2] in ("another", "ones"))
        if j > i and not pronoun:
            out.append((words[i][0], words[j - 1][1], value))
        i = max(j, i + 1)
    return out


def _parse_number(words: list[tuple[int, int, str]], i: int, text: str) -> tuple[int, Decimal]:
    """How far the words from i spell one number, and its value: "twenty five" 25, "a hundred and five" 105, "two thousand
    twenty six" 2026, "three point five" 3.5. A word that can't continue the number ends it: "five six" is two numbers."""
    def word(k: int) -> str:  # the k-th word, if only spaces or a hyphen separate it from the one before
        if k < len(words) and (k == i or re.fullmatch(r"[\s-]+", text[words[k - 1][1]:words[k][0]])):
            return words[k][2]
        return ""

    total, current, last, cap, k = 0, 0, "", 10**15, i
    while w := word(k):
        after = word(k + 1)
        if w in _SMALL:
            v = _SMALL[w]
            if last in ("unit", "teen") or last == "tens" and not 0 < v < 10:
                break
            current, last = current + v, "unit" if v < 10 else "teen"
        elif w in _TENS:
            if last in ("unit", "teen", "tens"):
                break
            current, last = current + _TENS[w], "tens"
        elif w == "hundred":
            if last not in ("unit", "teen", "a") or current >= 100:
                break
            current, last = max(current, 1) * 100, "hundred"
        elif w in _SCALES:
            if last not in ("unit", "teen", "tens", "hundred", "a") or _SCALES[w] >= cap:
                break
            total, current, cap, last = total + max(current, 1) * _SCALES[w], 0, _SCALES[w], "scale"
        elif w == "and" and last in ("hundred", "scale") and (after in _SMALL or after in _TENS):
            last = "and"
        elif w == "a" and k == i and (after == "hundred" or after in _SCALES):
            last = "a"
        elif w == "point" and last in ("unit", "teen", "tens", "hundred", "scale") and _SMALL.get(after, 10) < 10:
            digits, k = "", k + 1
            while _SMALL.get(word(k), 10) < 10:
                digits, k = digits + str(_SMALL[word(k)]), k + 1
            return k, Decimal(f"{total + current}.{digits}")
        else:
            break
        k += 1
    return k, Decimal(total + current)


# ---------------------------------------------------------------- words

# Words a cleanup may add or drop freely. Meaningful small words are checked apart: negations, opposites ("on", "off").
_STOP = set("""a an the i me my mine myself you your yours yourself yourselves he him his himself she her hers herself it its
itself we us our ours ourselves they them their theirs themselves this that these those there here am is are was were be been
being do does did doing have has had having to of in on at by for with from into onto as than then so and but because about
just really very also too not no nor never""".split())
_FILLERS = {"um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "eh", "hmm", "hm", "mm", "mhm", "oh", "basically", "literally",
            "えーと", "えっと", "えー", "あの", "あのー", "まあ"}
_FILLER_PAIRS = {("you", "know"), ("ya", "know"), ("i", "mean"), ("sort", "of"), ("kind", "of")}
_START_FILLERS = {"so", "well", "okay", "ok", "like", "yeah", "yep", "yup", "right", "alright", "anyway", "anyways"}  # opening
_END_FILLERS = {"yeah", "yep", "yup"}  # closing a clause: "that works for me, yeah"
_OPENERS = {"and", "but", "or"}  # "and yeah we can", "but like it works": a clause still opens after them
# "like" is a word after these (a verb: "I like it", "would like"; a comparison: "looks like rain", "something like that")
# or before "this", "that"...; anywhere else it is a filler ("I was like thinking", "check the like logs").
_LIKE_AFTER = {"i", "you", "we", "they", "people", "would", "to", "not", "really", "also", "still", "always", "do", "does",
               "did", "look", "looks", "looked", "looking", "sound", "sounds", "sounded", "seem", "seems", "seemed", "feel",
               "feels", "felt", "something", "anything", "nothing", "more", "much", "exactly"}
_LIKE_BEFORE = {"this", "that", "these", "those", "to"}
_DETERMINERS = {"the", "a", "an", "this", "that", "these", "those", "what", "which", "some", "any", "every", "each", "no", "all",
                "same", "other", "different", "my", "your", "our", "their", "his", "her", "its"}
_CONTRACTIONS = {"n't": "not", "'ll": "will", "'re": "are", "'ve": "have", "'m": "am", "'d": "would", "'s": ""}
_NT_BASES = {"wo": "will", "ca": "can", "sha": "shall", "ai": "is"}
_ALIASES = {"gonna": ("going", "to"), "wanna": ("want", "to"), "gotta": ("got", "to"), "kinda": ("kind", "of"),
            "sorta": ("sort", "of"), "lemme": ("let", "me"), "gimme": ("give", "me"), "dunno": ("do", "not", "know"),
            "cannot": ("can", "not"), "ok": ("okay",), "yeah": ("yes",), "yep": ("yes",), "yup": ("yes",), "nope": ("no",),
            "nah": ("no",), "alright": ("all", "right"), "till": ("until",), "til": ("until",), "cuz": ("because",),
            "thru": ("through",)}
# Small words with an opposite: a cleanup adds or drops them freely, so a swap is looked for apart ("turn it on" -> "off").
_OPPOSITES = [("on", "off"), ("in", "out"), ("and", "or"), ("with", "without"), ("to", "from"), ("up", "down"),
              ("before", "after"), ("more", "less"), ("above", "below"), ("over", "under")]
_NEGATING_PREFIXES = ("un", "in", "im", "il", "ir", "dis", "non", "mis", "de", "anti")
# Self-correction cues (§56). "cross": may correct the previous sentence; "cancel": drops the whole clause before it;
# "comma": a cue only when a comma or dash follows ("no, Friday", not "no problem").
_CUES = {("no", "wait"): "cross", ("wait", "no"): "cross", ("oh", "wait"): "cross", ("actually", "no"): "cross",
         ("scratch", "that"): "cancel", ("or", "rather"): "", ("make", "that"): "", ("i", "mean"): "", ("i", "meant"): "",
         ("correction",): "", ("sorry",): "sorry", ("actually",): "", ("rather",): "rather", ("no",): "comma",
         ("wait",): "comma"}
# "wait, make that 35", said without the comma too: the first word belongs to the cue, not to what it corrects.
_CUES.update({(first, *rest): "" for first in ("wait", "no", "sorry", "oh", "actually")
              for rest in (("make", "that"), ("i", "mean"))})
_NOT_SORRY = {"am", "is", "are", "was", "were", "be", "been", "so", "very", "really", "feel", "felt", "i", "say", "said"}
_AUX = {"am", "is", "are", "was", "were", "do", "does", "did", "have", "has", "had", "can", "could", "will", "would", "shall",
        "should", "may", "might", "must"}
_WH = {"what", "why", "how", "when", "where", "who", "whom", "which", "whose"}
_WH_NEXT = {"how": {"many", "much", "long", "often", "far", "about", "come", "soon", "old", "big"},
            "what": {"if", "about", "time", "kind", "else", "happened", "day"}, "why": {"not"}}
_SUBJECTS = {"i", "you", "we", "they", "he", "she", "it", "there", "this", "that", "these", "those", "the", "a", "an", "my",
             "your", "our", "their", "his", "her", "its", "anyone", "anybody", "anything", "someone", "somebody", "something",
             "everyone", "everybody", "everything", "any", "all", "y'all"}
_TAGS = {"right", "yeah", "okay", "ok", "correct", "huh", "eh"}  # "it's ready, right": may be asked or said
_GREETINGS = {"hey", "hi", "hello"}
_GROUPS = {"team", "guys", "everyone", "folks", "all", "there"}
_LEADING = _FILLERS | _START_FILLERS | {"and", "but"}  # skipped before the words that make a question
_REQUESTS = {"please", "kindly"}
_SENTENCE_END = re.compile(r"[.?!…][\"'”’)\]]*\s|[。？！]")
_CLAUSE_END = re.compile(r"[,;:()\"“”—–、]|\s-\s|[.?!…][\"'”’)\]]*\s|[。？！]")
_CJK = ((0x3040, 0x309F, "kana"), (0x30A0, 0x30FF, "kata"), (0x31F0, 0x31FF, "kata"), (0xFF66, 0xFF9F, "kata"),
        (0x3400, 0x4DBF, "han"), (0x4E00, 0x9FFF, "han"), (0xF900, 0xFAFF, "han"), (0x20000, 0x2FFFF, "han"))


@dataclass
class _Tok:
    text: str
    start: int
    end: int
    cut: bool = False  # written cut off ("go-"): a false start or a stutter
    norm: str = ""  # case-folded, with a straight apostrophe
    sent: int = 0  # the sentence it is in
    first: bool = False  # the first word of its sentence
    clause: bool = False  # the first word of a clause (after a comma, a full stop... or the text's first)


def _is_word(c: str) -> bool:
    return unicodedata.category(c)[0] in "LMN"  # letters with their combining marks (Tamil, Hindi), and digits


def _script(c: str) -> str:
    """The script of a character, for text written without spaces; "" for the long-vowel mark, part of the word before."""
    if c == "ー":
        return ""
    code = ord(c)
    return next((name for lo, hi, name in _CJK if lo <= code <= hi), "other")


def _tokenize(text: str) -> list[_Tok]:
    """The words of a text. Hyphenated words are split ("follow-up"); Japanese is split where the script changes."""
    toks: list[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        if not _is_word(text[i]):
            i += 1
            continue
        j = i + 1
        while j < n and (_is_word(text[j]) or text[j] in "'’" and j + 1 < n and _is_word(text[j + 1])):
            j += 1
        start, script = i, _script(text[i]) or "kata"
        for k in range(i + 1, j):
            if (s := _script(text[k])) and s != script:
                toks.append(_Tok(text[start:k], start, k))
                start, script = k, s
        toks.append(_Tok(text[start:j], start, j, cut=j < n and text[j] in "-–—" and (j + 1 == n or not _is_word(text[j + 1]))))
        i = j
    sent = 0
    for k, t in enumerate(toks):
        t.norm = t.text.casefold().replace("’", "'")
        gap = text[toks[k - 1].end:t.start] if k else ""
        if k and _SENTENCE_END.search(gap) and not (len(toks[k - 1].text) == 1 and toks[k - 1].text.islower()
                                                     and gap.startswith(".")):  # "5 p.m. tomorrow", "e.g. this"
            sent += 1
        t.sent, t.first = sent, not k or toks[k - 1].sent != sent
        t.clause = t.first or bool(_CLAUSE_END.search(gap))
    return toks


def _words(norm: str) -> tuple[str, ...]:
    """A written word as the words it stands for: "i'll" -> ("i", "will"), "can't" -> ("can", "not"), "gonna"..."""
    if norm in _ALIASES:
        return _ALIASES[norm]
    for suffix, full in _CONTRACTIONS.items():
        if norm.endswith(suffix) and len(norm) > len(suffix):
            base = norm[:-len(suffix)]
            base = _NT_BASES.get(base, base) if suffix == "n't" else base
            return (base, full) if full else (base,)  # "'s" may be is, has or a possessive: it is dropped
    return (norm,)


def _stem(word: str) -> str:
    """A rough stem, so "deploys", "deployed" and "deploying" are one word ("stopped" -> "stop", "making" -> "mak")."""
    if not word.isascii() or not word.isalpha() or len(word) <= 2:
        return word
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= (2 if suffix == "s" else 3) and not (
                suffix == "s" and word.endswith(("ss", "us", "is"))):
            word = word[:-len(suffix)]
            break
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiouls":
        word = word[:-1]
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


_HEDGES = {_stem(w) for w in ("maybe", "perhaps", "probably", "possibly", "likely", "unlikely", "might", "guess", "suppose",
                              "think", "believe", "apparently", "approximately", "roughly", "presumably")}


def _is_content(word: str) -> bool:
    return word not in _STOP and word not in _FILLERS and not any(c.isdigit() for c in word)


def _key(e: ProtectedEntity) -> tuple[str, str]:
    return e.kind, e.normalized


class _Side:
    """One text, analysed: its words and sentences, and the protected value each word is part of."""

    def __init__(self, text: str, terms: list[str]):
        self.text, self.toks = text, _tokenize(text)
        self.entities = extract_entities(text, terms)
        starts, ends = [t.start for t in self.toks], [t.end for t in self.toks]
        self.spans = [range(bisect.bisect_right(ends, e.span[0]), bisect.bisect_left(starts, e.span[1])) for e in self.entities]
        self.owner: list[int | None] = [None] * len(self.toks)  # the entity (not a negation) a word is part of
        for k, (e, span) in enumerate(zip(self.entities, self.spans, strict=True)):
            for i in span if e.kind != "NEGATION" else ():
                self.owner[i] = k if self.owner[i] is None else self.owner[i]
        self.words = [_words(t.norm) for t in self.toks]
        # Content words, stemmed: what must survive. Words inside a protected value are compared as that value instead.
        self.keys = [[_stem(w) for w in ws if _is_content(w)] if self.owner[i] is None else [] for i, ws in enumerate(self.words)]
        self.values = Counter(_key(e) for e in self.entities if e.kind != "NEGATION")
        self.stems = {_stem(w) for ws in self.words for w in ws}
        self.shown = {}  # stem -> the word as written, for the reasons
        for t, ks in zip(self.toks, self.keys, strict=True):
            for k in ks:
                self.shown.setdefault(k, t.text)
        self.sentences: list[tuple[int, int]] = []
        for i, t in enumerate(self.toks):
            if t.first:
                self.sentences.append((i, i))
            self.sentences[-1] = (self.sentences[-1][0], i)

    def gap(self, i: int) -> str:
        """The text before word i (after the last word when i is past the end)."""
        end = self.toks[i].start if i < len(self.toks) else len(self.text)
        return self.text[self.toks[i - 1].end if i else 0:end]

    def quote(self, indexes: Iterable[int]) -> str:
        return " ".join(self.toks[i].text for i in indexes)

    def question_mark(self, sentence: tuple[int, int]) -> bool:
        return bool(re.search(r"[?？]", self.gap(sentence[1] + 1)))

    def joins(self) -> dict[str, list[str]]:
        """Two neighbouring words written as one ("log in" -> "login", "e-mail" -> "email"): the joined stem -> their keys."""
        out = {}
        for i in range(len(self.toks) - 1):
            a, b = self.toks[i], self.toks[i + 1]
            if a.sent == b.sent and self.owner[i] is None and self.owner[i + 1] is None and (a.norm + b.norm).isalpha():
                out[_stem(a.norm + b.norm)] = self.keys[i] + self.keys[i + 1]
        return out


def _asks(side: _Side, sentence: tuple[int, int]) -> bool | None:
    """Is a sentence a question: a question mark, or worded as one ("can you send it", "where is it")? None when it may be
    either ("it's ready right")."""
    if side.question_mark(sentence):
        return True
    first, last = sentence
    for start in (k for k in range(first, last + 1) if side.toks[k].clause):  # "John, can you...", "if it fails, do we..."
        k = start
        while k <= last and side.toks[k].norm in _LEADING:
            k += 1
        if k < last and side.toks[k].norm in _GREETINGS:  # "hey John can you..."
            k += 1 + (side.toks[k + 1].text[:1].isupper() or side.toks[k + 1].norm in _GROUPS)
        if k > last:
            continue
        words, nxt = side.words[k], side.words[k + 1][0] if k < last else ""
        if words[0] in _WH and (  # "where is", "how many", "who sent", "what's" - not "when I get home"
                len(words) > 1 or side.toks[k].norm.endswith("'s") or nxt in _AUX or nxt in _WH_NEXT.get(words[0], ())
                or words[0] in ("who", "which", "whose") and nxt not in _SUBJECTS):
            return True
        if words[0] in _AUX and (nxt in _SUBJECTS or k < last and side.toks[k + 1].text[:1].isupper()) and not (
                words == ("do",) and nxt in ("it", "this", "that")):  # "do it now" is a command
            return True
    return None if last - first >= 2 and side.toks[last].norm in _TAGS else False


def _cue(b: _Side, i: int) -> tuple[int, str] | None:
    """A self-correction cue starting at word i: (its length in words, its kind)."""
    toks = b.toks
    for size in (3, 2, 1):
        words = tuple(t.norm for t in toks[i:i + size])
        kind = _CUES.get(words) if len(words) == size else None
        if kind is None or toks[i + size - 1].sent != toks[i].sent:
            continue
        prev, nxt = b.words[i - 1][-1] if i else "", toks[i + size].norm if i + size < len(toks) else ""
        if (kind == "comma" and not re.match(r"\s*[,—–…]", b.gap(i + size))
                or kind == "rather" and (prev in ("would", "had") or nxt == "than")
                or kind == "sorry" and (prev in _NOT_SORRY or nxt in ("about", "for", "to", "that", "if"))):
            continue
        return size, kind
    return None


def _reparandum(b: _Side, candidates: list[int], repair: list[int], kind: str) -> list[int]:
    """Which of the words before a cue its repair replaces."""
    toks = b.toks
    if kind == "cancel":  # "scratch that": the clause before it
        first = candidates[-1]
        while first > candidates[0] and not toks[first].clause:
            first -= 1
        return [k for k in candidates if k >= first][-12:]
    if back := [k for k in candidates[-6:] if toks[k].norm == toks[repair[0]].norm]:  # starts over: "to John, sorry, to Mary"
        return [k for k in candidates if k >= back[-1]]
    owner = b.owner[candidates[-1]]
    if owner is not None and b.owner[repair[0]] is not None:  # one value for another: "tomorrow, no wait, Friday"
        return [k for k in candidates if b.owner[k] == owner]
    if (new := b.owner[repair[0]]) is not None:  # a value with its noun: "five servers, wait, make that 35 (servers)"
        for k in reversed(candidates[-4:]):
            if (old := b.owner[k]) is not None:
                if b.entities[old].kind == b.entities[new].kind:
                    return [j for j in candidates if j >= b.spans[old][0]]
                break
    return candidates[-1:]


def _excused(b: _Side, a: _Side) -> tuple[set[int], list[str]]:
    """The words of `before` the cleanup may drop, and notes on the self-corrections it applied."""
    toks, n, out, notes = b.toks, len(b.toks), set(), []
    at_start, joined, i = True, False, 0  # opening a clause; right after "and", "but" or "or"
    while i < n:  # fillers
        t, prev = toks[i], b.words[i - 1][-1] if i else ""
        nxt = toks[i + 1].norm if i + 1 < n else ""
        at_start = at_start or t.clause
        if (pair := (t.norm, nxt) if nxt else ()) in _FILLER_PAIRS and not (
                pair[1] == "of" and prev in _DETERMINERS - {"some"}):  # "kind of", "some kind of"; not "what kind of car"
            out.update((i - (prev == "some" and pair[1] == "of"), i, i + 1))
            i += 2
            continue
        like = t.norm == "like" and (  # "it was, like, big", "um like", "I was like thinking": a filler; "I like it": a word
            "," in b.gap(i) + b.gap(i + 1) or prev in _FILLERS or nxt in _FILLERS
            or prev not in _LIKE_AFTER and nxt not in _LIKE_BEFORE)
        kinda = t.norm in ("kinda", "sorta") and prev not in _DETERMINERS - {"some"}  # "it's kinda slow"
        last = i + 1 == n or toks[i + 1].clause
        if (t.norm in _FILLERS or at_start and t.norm in _START_FILLERS or last and t.norm in _END_FILLERS or like
                or kinda or t.norm == "y'know" or joined and t.norm in _START_FILLERS - {"right", "well"}):
            out.update((i - 1, i) if kinda and prev == "some" else (i,))
        elif t.norm in _OPENERS:  # "and yeah", "but so": still opening ("left and right" keeps its "right")
            joined = True
        else:
            at_start = joined = False
        i += 1
    said = [i for i in range(n) if i not in out]
    for size in (3, 2, 1):  # repeats, fillers aside: "we need to we need to", "the the", "I uh I", "project. Project"
        for p in range(len(said) - 2 * size + 1):
            run = said[p:p + 2 * size]
            if all(toks[run[k]].norm == toks[run[size + k]].norm for k in range(size)):
                out.update(run[:size])
    for i, t in enumerate(toks):  # cut-off words: a stutter ("th- the"), or a false start given up for a restart
        if t.cut:
            out.add(i)
            restart = toks[i + 1].norm if i + 1 < n else None
            for s in range(i - 1, max(i - 7, -1), -1):  # "I was go- I went": the restart repeats where the false start began
                if toks[s].sent != t.sent:
                    break
                if toks[s].norm == restart:
                    out.update(range(s, i))
                    break
    i = 0
    while i < n:  # self-corrections
        if not (cue := _cue(b, i)):
            i += 1
            continue
        size, kind = cue
        end = i + size
        if kind == "cross":
            out.update(range(i, end))  # "no wait" is never a negation or something said
        lo = b.sentences[toks[i].sent][0]
        candidates = [k for k in range(lo, i) if k not in out]
        if not candidates and kind in ("cross", "cancel") and toks[i].sent:  # "...tomorrow. No wait, Friday."
            candidates = [k for k in range(b.sentences[toks[i].sent - 1][0], i) if k not in out]
        repair = [k for k in range(end, min(n, end + 8)) if k not in out]
        if candidates and repair:
            replaced = _reparandum(b, candidates, repair, kind)
            anchor = next((k for k in repair[:4] if b.owner[k] is not None or b.keys[k]), repair[0])
            # Only a correction the LLM applied excuses anything: its replacement must be in `after`.
            if (_key(b.entities[b.owner[anchor]]) in a.values if b.owner[anchor] is not None
                    else set(b.keys[anchor] or map(_stem, b.words[anchor])) & a.stems):
                out.update(replaced, range(i, end))
                notes.append(f"self-correction: '{b.quote(replaced)}' -> '{b.quote(repair)}'")
        i = end
    return out, notes


def _variant(x: str, y: str) -> bool:
    """Two spellings of one word ("colour", "color"), not two words ("increase", "decrease") or opposites ("able", "unable")."""
    short, long = sorted((x, y), key=len)
    if long.endswith(short) and long[:len(long) - len(short)] in _NEGATING_PREFIXES:
        return False
    return x[:2] == y[:2] and difflib.SequenceMatcher(None, x, y).ratio() >= 0.8


def _reconcile(b: _Side, a: _Side, removed: list[str], added: list[str]) -> None:
    """Cancel out what isn't a change of words: a compound written apart or together ("log in", "login"), and two spellings
    of one word."""
    for side, joined, parts in ((b, added, removed), (a, removed, added)):
        joins = side.joins()
        for k in [k for k in joined if k in joins]:
            joined.remove(k)
            for part in joins[k]:
                if part in parts:
                    parts.remove(part)
    for r in list(removed):
        if (v := next((x for x in added if _variant(r, x)), None)) is not None:
            removed.remove(r)
            added.remove(v)


def _swapped_fillers(b: _Side, a: _Side, excused: set[int], added: set[str]) -> list[str]:
    """Words the cleanup may drop (fillers, repeats...) that it replaced with new words where they stood instead: "go,
    yeah" -> "go PC" is a change, not a removal."""
    if not added:
        return []
    matcher = difflib.SequenceMatcher(None, [t.norm for t in b.toks], [t.norm for t in a.toks], autojunk=False)
    swaps = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        gone, new = [i for i in range(i1, i2) if i in excused], [j for j in range(j1, j2) if added & set(a.keys[j])]
        if op == "replace" and gone and new:
            swaps.append(f"'{b.quote(gone)}' with '{a.quote(new)}'")
    return swaps


def _change_ratio(before: str, after: str) -> float:
    if before == after:
        return 0.0
    if len(before) + len(after) > 1500:  # matching characters is quadratic (0.3 s at 3500): long texts go word by word
        return 1 - difflib.SequenceMatcher(None, before.split(), after.split(), autojunk=False).ratio()
    return 1 - difflib.SequenceMatcher(None, before, after, autojunk=False).ratio()


class Guard:
    """Decides whether the LLM's text may be typed instead of the formatted text it was given. validate() never raises."""

    def __init__(self, config: GuardConfig | None = None):
        self.config = config or GuardConfig()

    def validate(self, before: str, after: str, terms: Iterable[str] = ()) -> GuardResult:
        """`before`: the trusted formatted text; `after`: the LLM's; `terms`: the user's dictionary terms, kept as written.
        Rejected (accepted=False) with the reasons; diagnostics name the rules that fired (`rules`, the first in `rule`),
        the words added, dropped and excused, and the protected values lost or new."""
        try:
            return self._validate(before, after, terms)
        except Exception as e:  # a guard that fails must fail closed: the formatted text is typed
            log.exception("The guard failed on %r -> %r", before, after)
            return _result([("error", f"the guard failed ({e.__class__.__name__})")], [], 1.0, {})

    def _validate(self, before: str, after: str, terms: Iterable[str]) -> GuardResult:
        terms = _unique(terms)
        b, a = _Side(before, terms), _Side(after, terms)
        ratio = _change_ratio(before, after)
        strict = ratio > self.config.max_change_ratio  # "inspect more carefully" (§66): fewer words may change
        fired: list[tuple[str, str]] = []
        diagnostics: dict = {"change_ratio": round(ratio, 3), "scrutinized": strict}
        if not b.toks or not a.toks:
            if a.toks:
                fired.append(("invented", "the LLM wrote text where nothing was said"))
            elif b.toks:
                fired.append(("empty", "the LLM returned no text"))
            return _result(fired, [], ratio, diagnostics)
        excused, notes = _excused(b, a)

        # Protected values (§62-64): none lost (unless a correction replaced it), none new.
        def is_excused(span: range) -> bool:
            return bool(span) and all(i in excused for i in span)

        needed, known = Counter(), Counter()
        for e, span in zip(b.entities, b.spans, strict=True):
            if e.kind != "NEGATION":
                known[_key(e)] += 1
                needed[_key(e)] += not is_excused(span)
        missing, new = needed - a.values, a.values - known
        shown = {_key(e): e.value for e in reversed(a.entities + b.entities)}
        fired += [("entity_missing", f"lost {k[0].lower()} '{shown[k]}'") for k in missing]
        fired += [("entity_added", f"new {k[0].lower()} '{shown[k]}'") for k in new]
        for term in terms:  # the user's spelling of their own words: "GitHub" stays "GitHub"
            if _spelled(a, term) < _spelled(b, term, is_excused) and ("TERM", term.casefold()) not in missing:
                fired.append(("term_respelled", f"changed how '{term}' is written"))
        negations = [span for e, span in zip(b.entities, b.spans, strict=True) if e.kind == "NEGATION"]
        n_needed, n_after = sum(not is_excused(span) for span in negations), sum(e.kind == "NEGATION" for e in a.entities)
        if n_after < n_needed:
            fired.append(("negation", "dropped a negation"))
        elif n_after > len(negations):
            fired.append(("negation", "added a negation"))

        # The speech act (§57): a question stays a question, a statement or a command doesn't become one, nor a request.
        asked = [_asks(b, s) for s in b.sentences]
        q_before, q_after = asked.count(True), sum(a.question_mark(s) for s in a.sentences)
        if self.config.semantic_check_enabled:
            if q_before and not q_after:
                fired.append(("question_lost", "turned a question into a statement"))
            elif q_after and None not in asked and not q_before:
                fired.append(("question_added", "turned a statement into a question"))
            p_before, p_after = (sum(w in _REQUESTS for ws in side.words for w in ws) for side in (b, a))
            if bool(p_before) != bool(p_after):
                fired.append(("request", "turned a command into a request" if p_after else "dropped 'please'"))

        # Words (§65, §67): small words with an opposite, then content words.
        counts_b, counts_a = (Counter(w for ws in side.words for w in ws) for side in (b, a))
        for x, y in _OPPOSITES:
            if counts_b[x] > counts_a[x] and counts_a[y] > counts_b[y] or counts_b[y] > counts_a[y] and counts_a[x] > counts_b[x]:
                fired.append(("opposite", f"swapped '{x}' and '{y}'"))
        all_b, all_a = (Counter(k for ks in side.keys for k in ks) for side in (b, a))
        needed_b = Counter(k for i, ks in enumerate(b.keys) if i not in excused for k in ks)
        removed, added = list((needed_b - all_a).elements()), list((all_a - all_b).elements())
        _reconcile(b, a, removed, added)
        if removed and added:
            fired.append(("substitution", "replaced " + ", ".join(f"'{b.shown[r]}' with '{a.shown[x]}'"
                                                                  for r, x in zip(removed, added, strict=False))))
        elif swaps := _swapped_fillers(b, a, excused, set(added)):  # a filler may go, not become a new word
            fired.append(("substitution", "replaced " + ", ".join(swaps)))
        proper = {k for i, t in enumerate(b.toks) if not t.first and t.text[:1].isupper() and b.words[i][0] != "i"
                  for k in b.keys[i]}
        if names := [b.shown[k] for k in removed if k in proper]:
            fired.append(("name_removed", "dropped " + ", ".join(f"'{w}'" for w in names)))
        h_needed, h_before, h_after = (sum(_stem(w) in _HEDGES for i, ws in enumerate(side.words) if i not in skip for w in ws)
                                       for side, skip in ((b, excused), (b, ()), (a, ())))
        if h_after < h_needed:
            fired.append(("hedge", "dropped uncertainty (maybe, probably, I think...)"))
        elif h_after > h_before:
            fired.append(("hedge", "added uncertainty"))
        if self.config.semantic_check_enabled:
            for first, last in a.sentences:  # a sentence of nothing but new words: "Please review it before the meeting."
                keys = {k for i in range(first, last + 1) for k in a.keys[i]}
                values = {_key(a.entities[a.owner[i]]) for i in range(first, last + 1) if a.owner[i] is not None}
                if keys and not keys & b.stems and not values & set(known):
                    fired.append(("added_sentence", f"added '{a.text[a.toks[first].start:a.toks[last].end]}'"))
        max_added = max(0, self.config.max_added_words - 1) if strict else self.config.max_added_words
        if len(added) > max_added:
            fired.append(("added_words", f"added {len(added)} words: " + ", ".join(a.shown[k] for k in added)))
        if len(removed) > (0 if strict else 1):
            fired.append(("removed_words", f"dropped {len(removed)} words: " + ", ".join(b.shown[k] for k in removed)))
        if len(a.toks) > 1.5 * len(b.toks) + 5:
            fired.append(("length", f"{len(a.toks)} words for {len(b.toks)}"))

        diagnostics.update(
            added_words=[a.shown[k] for k in added], removed_words=[b.shown[k] for k in removed],
            excused_words=[b.toks[i].text for i in sorted(excused)], corrections=notes,
            missing_entities=[f"{kind}:{value}" for kind, value in missing.elements()],
            new_entities=[f"{kind}:{value}" for kind, value in new.elements()],
            negations={"before": len(negations), "required": n_needed, "after": n_after},
            questions={"before": q_before, "after": q_after})
        if strict:
            notes = [*notes, f"changed a lot ({ratio:.0%}): checked more strictly"]
        return _result(fired, notes, ratio, diagnostics)


def _spelled(side: _Side, term: str, is_excused=None) -> int:
    """How often a term is written exactly as the user spells it (not counting words a correction replaced)."""
    return sum(1 for e, span in zip(side.entities, side.spans, strict=True)
               if e.kind == "TERM" and _same_spelling(" ".join(e.value.split()), term)
               and not (is_excused and is_excused(span)))


def _same_spelling(written: str, term: str) -> bool:
    """Written as the user spells the term: either apostrophe, and a lowercase term may start a sentence with a capital
    ("tray" -> "Tray"); the user's own capitals ("GitHub") must stay as they are."""
    written, term = written.replace("’", "'"), term.replace("’", "'")
    return written == term or (term[:1].islower() and written == term[:1].upper() + term[1:])


def _result(fired: list[tuple[str, str]], notes: list[str], ratio: float, diagnostics: dict) -> GuardResult:
    diagnostics["rules"] = list(dict.fromkeys(rule for rule, _ in fired))
    diagnostics["rule"] = fired[0][0] if fired else ""
    return GuardResult(not fired, [reason for _, reason in fired] or notes, ratio, diagnostics)
