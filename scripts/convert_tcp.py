"""
scripts/convert_tcp.py

Turns an EEBO-TCP text (the Text Creation Partnership's keyboarded early English books, released under
Creative Commons 0) into the plain-text layout scripts/ingest_book.py reads: "## <chapter title>" lines, then
paragraphs separated by blank lines. First used for Jeremiah Burroughs' Irenicum (TCP A30587), added for
Art. 11(K), the unity of believers (Keziah approved, 2026-10-18).

Faithful to the text. The only changes, as agreed: old letter forms that aren't spelling (the long s "ſ" -> "s",
"Ʋ"/"ʋ" -> "U"/"u"), words split by a line-end hyphen rejoined, and the printer's margin notes and gaps
(untranscribed foreign script) dropped. Spelling itself ("evill", "doe") is left as written.

    python scripts/convert_tcp.py <downloaded A30587.xml> corpus/burroughs_irenicum.txt
"""

import html
import re
import sys


def _clean(fragment):
    fragment = re.sub(r"<note\b.*?</note>", " ", fragment, flags=re.S)
    fragment = re.sub(r"<gap\b.*?</gap>", " ", fragment, flags=re.S)
    fragment = re.sub(r'<g ref="char:EOLhyphen"\s*/>\s*', "", fragment)       # "empti-\nneſſe" -> "emptineſſe"
    fragment = re.sub(r"<[^>]+>", "", fragment)
    text = html.unescape(fragment)
    text = text.replace("ſ", "s").replace("Ʋ", "U").replace("ʋ", "u")
    return re.sub(r"\s+", " ", text).strip()


def convert(xml):
    body = xml.split("<text", 1)[1]
    out = []
    for chapter in re.findall(r'<div n="\d+" type="chapter">(.*?)(?=<div n="\d+" type="chapter">|</body>)', body, re.S):
        heads = re.findall(r"<head(?: [^>]*)?>(.*?)</head>", chapter, re.S)
        sub = re.search(r'<head type="sub">(.*?)</head>', chapter, re.S)
        title = _clean(sub.group(1) if sub else (heads[0] if heads else "Untitled"))
        out.append(f"## {title.rstrip('.')}")
        # Section headings inside the chapter (e.g. "The Pride of mens hearts...") stay, as short paragraphs.
        for kind, content in re.findall(r"<(head|p)\b[^>]*>(.*?)</\1>", chapter, re.S):
            text = _clean(content)
            if text and not re.fullmatch(r"CHAP\.? ?[IVXL]+\.?", text) and text.rstrip(".") != title.rstrip("."):
                out.append(text)
    return "\n\n".join(out) + "\n"


if __name__ == "__main__":
    source, target = sys.argv[1], sys.argv[2]
    text = convert(open(source, encoding="utf-8").read())
    open(target, "w", encoding="utf-8").write(text)
    print(f"{target}: {text.count(chr(10) + '## ') + text.startswith('## ')} chapters, {len(text.split())} words")
