"""
app/services/bible_refs.py

Finds Bible references in a member's question -- "Romans 12:2", "Rom 12:2-5", "1 Cor 13", "John 3 verse 16",
"Psalm 23", and the Swahili book names members use ("Warumi 12:2", "Yohana 3:16", "Zaburi 23") -- so the
companion can look the exact verses up instead of hoping a meaning-search finds them (Keziah, 2026-10-12).
No AI involved: a fixed list of book names, abbreviations and Swahili names.

Book names follow the Berean Standard Bible's own spelling (its file says "Psalm", "Song of Solomon"), so a
parsed reference matches the bible_verses table exactly.
"""

import re

# (canonical name as in the BSB file, extra names / abbreviations / Swahili names)
_BOOKS = [
    ("Genesis", ["gen", "gn", "mwanzo"]), ("Exodus", ["exod", "exo", "kutoka"]),
    ("Leviticus", ["lev", "lv", "mambo ya walawi", "walawi"]), ("Numbers", ["num", "nm", "hesabu"]),
    ("Deuteronomy", ["deut", "dt", "kumbukumbu la torati", "kumbukumbu"]), ("Joshua", ["josh", "jos", "yoshua"]),
    ("Judges", ["judg", "jdg", "waamuzi"]), ("Ruth", ["rut", "ruthu"]),
    ("1 Samuel", ["1 sam", "1 sa", "1 samweli"]), ("2 Samuel", ["2 sam", "2 sa", "2 samweli"]),
    ("1 Kings", ["1 kgs", "1 ki", "1 wafalme"]), ("2 Kings", ["2 kgs", "2 ki", "2 wafalme"]),
    ("1 Chronicles", ["1 chron", "1 chr", "1 mambo ya nyakati"]), ("2 Chronicles", ["2 chron", "2 chr", "2 mambo ya nyakati"]),
    ("Ezra", ["ezr"]), ("Nehemiah", ["neh", "nehemia"]), ("Esther", ["esth", "est", "esta"]), ("Job", ["ayubu"]),
    ("Psalm", ["psalms", "ps", "psa", "pss", "zaburi"]), ("Proverbs", ["prov", "prv", "pr", "mithali"]),
    ("Ecclesiastes", ["eccl", "ecc", "qoheleth", "mhubiri"]),
    ("Song of Solomon", ["song of songs", "song", "sos", "wimbo ulio bora"]),
    ("Isaiah", ["isa", "isaya"]), ("Jeremiah", ["jer", "yeremia"]), ("Lamentations", ["lam", "maombolezo"]),
    ("Ezekiel", ["ezek", "eze", "ezekieli"]), ("Daniel", ["dan", "dn", "danieli"]), ("Hosea", ["hos"]),
    ("Joel", ["jl", "yoeli"]), ("Amos", ["amosi"]), ("Obadiah", ["obad", "ob", "obadia"]), ("Jonah", ["jon", "yona"]),
    ("Micah", ["mic", "mika"]), ("Nahum", ["nah", "nahumu"]), ("Habakkuk", ["hab", "habakuki"]),
    ("Zephaniah", ["zeph", "zep", "sefania"]), ("Haggai", ["hag", "hagai"]), ("Zechariah", ["zech", "zec", "zekaria"]),
    ("Malachi", ["mal", "malaki"]),
    ("Matthew", ["matt", "mat", "mt", "mathayo"]), ("Mark", ["mrk", "mk", "marko"]), ("Luke", ["luk", "lk", "luka"]),
    ("John", ["jn", "jhn", "yohana"]), ("Acts", ["act", "matendo ya mitume", "matendo"]),
    ("Romans", ["rom", "rm", "warumi"]),
    ("1 Corinthians", ["1 cor", "1 co", "1 wakorintho"]), ("2 Corinthians", ["2 cor", "2 co", "2 wakorintho"]),
    ("Galatians", ["gal", "wagalatia"]), ("Ephesians", ["eph", "waefeso"]), ("Philippians", ["phil", "php", "wafilipi"]),
    ("Colossians", ["col", "wakolosai"]),
    ("1 Thessalonians", ["1 thess", "1 thes", "1 th", "1 wathesalonike"]),
    ("2 Thessalonians", ["2 thess", "2 thes", "2 th", "2 wathesalonike"]),
    ("1 Timothy", ["1 tim", "1 ti", "1 timotheo"]), ("2 Timothy", ["2 tim", "2 ti", "2 timotheo"]),
    ("Titus", ["tit", "tito"]), ("Philemon", ["philem", "phm", "filemoni"]), ("Hebrews", ["heb", "waebrania"]),
    ("James", ["jas", "jm", "yakobo"]), ("1 Peter", ["1 pet", "1 pe", "1 petro"]), ("2 Peter", ["2 pet", "2 pe", "2 petro"]),
    ("1 John", ["1 jn", "1 jhn", "1 yohana"]), ("2 John", ["2 jn", "2 jhn", "2 yohana"]), ("3 John", ["3 jn", "3 jhn", "3 yohana"]),
    ("Jude", ["jud", "yuda"]), ("Revelation", ["revelations", "rev", "rv", "ufunuo"]),
]
BOOK_ORDER = [name for name, _ in _BOOKS]

# Book names that are also everyday words ("my job 2 years ago", "mark 4 of 10", "song 3"): a CHAPTER on its
# own only counts as a reference when the name is capitalised ("Job 2"); with a verse ("job 3:1") it always does.
# (The 2-letter abbreviations "is", "am", "ex" are left out entirely for the same reason.)
_EVERYDAY_WORDS = {"job", "mark", "acts", "act", "song", "numbers", "judges", "num", "hag", "mal", "col", "tit", "jud"}

_NUMBER_WORDS = {"1": "1", "i": "1", "1st": "1", "first": "1", "2": "2", "ii": "2", "2nd": "2", "second": "2",
                 "3": "3", "iii": "3", "3rd": "3", "third": "3"}

_ALIASES = {}
for _name, _extra in _BOOKS:
    for _alias in [_name.lower()] + _extra:
        _ALIASES[_alias] = _name

# "1 corinthians" may also be written "i corinthians", "1st corinthians", "first corinthians", "1corinthians".
_NUMBERED = {}
for _alias, _name in list(_ALIASES.items()):
    m = re.match(r"^([123]) (.+)$", _alias)
    if m:
        _NUMBERED.setdefault(m.group(2), {})[m.group(1)] = _name

_unnumbered = sorted((a for a in _ALIASES if not re.match(r"^[123] ", a)), key=len, reverse=True)
_bases = sorted(_NUMBERED, key=len, reverse=True)
# Roman numerals and words need a space after them ("Isa 53:5" is Isaiah, not "I Sa" = 1 Samuel); digits may not.
_NUM = r"(?:(?P<num>1st|2nd|3rd|first|second|third|iii|ii|i)\s+|(?P<dnum>[123])\s*)"
_REF = re.compile(
    rf"(?<![\w])(?:{_NUM}(?P<nbook>{'|'.join(map(re.escape, _bases))})|(?P<book>{'|'.join(map(re.escape, _unnumbered))}))"
    r"\.?\s+(?P<ch>\d{1,3})"
    r"(?:\s*[:.]\s*(?P<v1>\d{1,3})(?:\s*[-–]\s*(?P<v2>\d{1,3}))?"
    r"|\s*,?\s+(?:verses?|vv?\.?|mstari)\s*(?P<v1w>\d{1,3})(?:\s*(?:-|–|to)\s*(?P<v2w>\d{1,3}))?)?"
    r"(?![\w:])",
    re.IGNORECASE)


def find_references(text):
    """
    [(book, chapter, first_verse or None, last_verse or None), ...] in the order they appear.
    A chapter alone ("Psalm 23") has no verses: the whole chapter is meant.
    """
    found = []
    for m in _REF.finditer(text or ""):
        if m.group("nbook"):
            number = _NUMBER_WORDS[(m.group("num") or m.group("dnum")).lower()]
            book = _NUMBERED[m.group("nbook").lower()].get(number)
            if book is None:
                continue          # "3 Corinthians" -- no such book
        else:
            book = _ALIASES[m.group("book").lower()]
        v1 = m.group("v1") or m.group("v1w")
        v2 = m.group("v2") or m.group("v2w")
        name = m.group("book")
        if not v1 and name and name.lower() in _EVERYDAY_WORDS and not name[0].isupper():
            continue          # "my job 2 years ago" -- not a reference
        first = int(v1) if v1 else None
        last = int(v2) if v2 else first
        if first and last and last < first:
            first, last = last, first
        found.append((book, int(m.group("ch")), first, last))
    return found


def citation(book, chapter, first, last):
    """'Romans 12:1-4 (BSB)', 'Psalm 23 (BSB)'."""
    if first is None:
        return f"{book} {chapter} (BSB)"
    return f"{book} {chapter}:{first}" + (f"-{last}" if last and last != first else "") + " (BSB)"
