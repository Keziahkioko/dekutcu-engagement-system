"""
scripts/convert_henry.py

Turns downloaded CCEL pages of Matthew Henry's Commentary on the Whole Bible (1706-1714; Romans to Revelation
completed from his notes by fellow nonconformist ministers after his death -- public domain) into the plain-text
layout scripts/ingest_book.py reads: "## <section title>" lines, then paragraphs separated by blank lines.

Only SELECTED chapters are used (Keziah approved, 2026-10-18), chosen for the doctrinal-basis points the coverage
check found weak: Art. 11(K) unity without discrimination, (I) the second coming, (B) the final judgment, and
(J) the Church. Each commentary section becomes one "chapter", titled with its verses and Henry's own heading:
"James 2:1-7 -- Regard Due to Poor Christians; Partiality Condemned".

    python scripts/convert_henry.py <folder of downloaded henry_*.html pages> corpus/henry_selected.txt
"""

import glob
import html
import os
import re
import sys

# CCEL's book codes -> the names members know. Order = the order in the output file. 2 Thessalonians 1,
# Philippians 3, Titus 2, 1 John 3 and Matthew 24 added the same day for Art. 11(I), after the re-check.
BOOKS = [("Matt", "Matthew"), ("John", "John"), ("Acts", "Acts"), ("Rom", "Romans"), ("iCor", "1 Corinthians"),
         ("iiCor", "2 Corinthians"), ("Gal", "Galatians"), ("Eph", "Ephesians"), ("Col", "Colossians"),
         ("iTh", "1 Thessalonians"), ("iiTh", "2 Thessalonians"), ("Phi", "Philippians"), ("Tit", "Titus"),
         ("Jam", "James"), ("iiPet", "2 Peter"), ("iJo", "1 John")]


def _text(fragment):
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"\s+", " ", html.unescape(fragment)).replace("—", " -- ").strip()


def _verses(div_id, book):
    """'Bible:Jas.2.1-Jas.2.7' -> 'James 2:1-7'."""
    m = re.match(r"Bible:\w+\.(\d+)\.(\d+)(?:-\w+\.(\d+)\.(\d+))?", div_id)
    if not m:
        return book
    ch1, v1, ch2, v2 = m.groups()
    if not ch2 or (ch2 == ch1 and v2 == v1):
        return f"{book} {ch1}:{v1}"
    return f"{book} {ch1}:{v1}-{v2}" if ch2 == ch1 else f"{book} {ch1}:{v1}-{ch2}:{v2}"


def convert_page(page, book):
    sections = []
    intro = re.search(r'<p class="intro"[^>]*>(.*?)</p>', page, re.S)
    chapter = re.search(r"CHAP\. ([IVXL]+)\.", page)
    for div_id, body in re.findall(r'<div class="Commentary" id="([^"]+)">(.*?)(?=<div class="Commentary"|<table[^>]*book_navbar_bottom)',
                                   page, re.S):
        heading = re.search(r"<h4[^>]*>(.*?)</h4>", body, re.S)
        title = _verses(div_id, book)
        if heading:
            name = re.sub(r"\(\s*a\.\s*d\.\s*\d+\.?\s*\)", "", _text(heading.group(1))).strip(" .")
            title += f" -- {name}"
        paragraphs = [_text(p) for p in re.findall(r'<p class="(?:passage|indent)"[^>]*>(.*?)</p>', body, re.S)]
        paragraphs = [p for p in paragraphs if p]
        if not sections and intro:
            paragraphs.insert(0, _text(intro.group(1)))     # Henry's summary of the chapter goes with its first section
        if paragraphs:
            sections.append((title, paragraphs))
    return sections


def main(folder, target):
    out, count = [], 0
    for code, book in BOOKS:
        for path in sorted(glob.glob(os.path.join(folder, f"henry_{code}_*.html")),
                           key=lambda p: int(re.search(r"_(\d+)\.html$", p).group(1))):
            for title, paragraphs in convert_page(open(path, encoding="utf-8", errors="replace").read(), book):
                out.append(f"## {title}")
                out.extend(paragraphs)
                count += 1
    text = "\n\n".join(out) + "\n"
    open(target, "w", encoding="utf-8").write(text)
    print(f"{target}: {count} sections, {len(text.split())} words")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
