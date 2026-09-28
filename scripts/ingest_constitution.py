"""
scripts/ingest_constitution.py

Stage 12: prepares the DeKUTCU Constitution for the RAG companion --
extracts the PDF's text, splits it into chunks, embeds each chunk
(Cloudflare bge-large-en-v1.5), and stores them in Neon (pgvector).
Run locally, once per new version of the constitution -- re-running
replaces the previous version atomically (see rag.replace_document).

    python scripts/ingest_constitution.py --dry-run   # show the chunks only
    python scripts/ingest_constitution.py             # embed + store

Chunking plan (agreed with Keziah, see PROJECT_LOG.md Stage 12):
  - one chunk per article, cited "DeKUTCU Constitution, Art. 25: Eligibility";
  - the doctrinal basis (Art. 11) one chunk PER CLAUSE A-K, so doctrinal
    questions retrieve the exact tenet and cite it as "Art. 11(F)";
  - long articles split into groups of whole clauses, each part keeping
    its article heading so it still makes sense on its own;
  - Preamble, Supremacy Clause and the definitions list as their own chunks;
  - cover page and table of contents skipped (they'd only produce
    misleading matches).

Article titles and chapter headings are read from the constitution's OWN
table of contents rather than hard-coded, so the script follows the
document. Every chunk's text starts with a SHORT heading -- just
"Article 11: Doctrinal Basis." -- and deliberately NOT the chapter name,
the document name, or the doctrinal basis's shared opening sentence.
Measured, not assumed: the first version repeated ~30 identical words
("DeKUTCU Constitution. Chapter One: DeKUTCU Charter. Article 11 ... The
doctrinal basis of the union shall be the fundamental truths of
Christianity including the following:") in front of every ~10-word
tenet, which made all 11 doctrinal chunks near-identical in meaning-space
and hid the part that makes each one different. See PROJECT_LOG.md
Stage 12 for the retrieval experiments.

The PDF extracts one word per line, so all text is rejoined into normal
whitespace first.
"""

import os
import re
import sys
import argparse
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from pypdf import PdfReader

PDF_PATH = os.path.join(PROJECT_ROOT, "corpus", "DeKUTCU CONSTITUTION.pdf")
TITLE = "DeKUTCU Constitution (2024)"
TIER = "constitution"
SOURCE = "corpus/DeKUTCU CONSTITUTION.pdf -- DeKUTCU's own constitution, adopted at the 2024 AGM (Art. 74)"
CITE = "DeKUTCU Constitution"

DOCTRINAL_BASIS_ARTICLE = 11
MAX_CHUNK_WORDS = 200  # comfortably inside bge-large's 512-token input limit, heading included

# \s*, not single spaces -- the PDF puts every word on its own line, so a
# footer really reads "Page\n|\n2". Matching only spaces (the first version)
# left footers inside chunk text AND glued one onto the first table-of-
# contents entry of each page, hiding Articles 15, 36, 55 and 72.
_FOOTER = re.compile(r"Page\s*\|\s*\d+")
_TOC_ENTRY_SPLIT = re.compile(r"\s*\.{3,}\s*[ivx\d]+\s*")
_ARTICLE_ENTRY = re.compile(r"^Article (\d+):\s*(.+?)\.?$")
_CHAPTER_ENTRY = re.compile(r"^CHAPTER [A-Z]+:?$")


def _heading_case(text):
    """'DeKUTCU CHARTER' -> 'DeKUTCU Charter': all-caps words are capitalised, mixed-case names (DeKUTCU) kept as written."""
    return " ".join(w.capitalize() if w.isupper() else w for w in text.split())


def _normalise(text):
    return " ".join(_FOOTER.sub(" ", text or "").split())


def _words(text):
    return len(text.split())


def read_pages():
    return [_normalise(page.extract_text()) for page in PdfReader(PDF_PATH).pages]


def _is_toc_page(text):
    return text.count("....") >= 3


def parse_toc(pages):
    """
    From the table-of-contents pages: {article number: (title, chapter)},
    plus every non-article heading (chapter and section headings) so they
    can be stripped from the body text where they sit between articles.
    """
    toc_text = " ".join(p for p in pages if _is_toc_page(p))
    entries = [e.strip() for e in _TOC_ENTRY_SPLIT.split(toc_text) if e.strip()]

    articles, headings = {}, []
    chapter, awaiting_chapter_title = None, False
    for entry in entries:
        match = _ARTICLE_ENTRY.match(entry)
        if match:
            articles[int(match.group(1))] = (match.group(2).strip(), chapter)
            continue
        headings.append(entry)
        if _CHAPTER_ENTRY.match(entry):
            chapter, awaiting_chapter_title = _heading_case(entry.rstrip(":")), True
        elif awaiting_chapter_title:
            chapter = f"{chapter}: {_heading_case(entry.rstrip('.'))}"
            awaiting_chapter_title = False
    return articles, headings


def _strip_trailing_headings(text, headings):
    """Removes chapter/section headings that sit at the END of an article's text (they belong to what follows)."""
    changed = True
    while changed:
        changed = False
        for heading in headings:
            if text.endswith(heading):
                text, changed = text[: -len(heading)].rstrip(), True
    return text


def _strip_title(body, title):
    """The body starts with the article's own title (e.g. 'Doctrinal Basis.') -- drop it, case-insensitively."""
    if body.lower().startswith(title.lower()):
        return body[len(title):].lstrip(" .:")
    return body


def split_clauses(body):
    """
    Splits an article body into (stem, [(letter, clause text), ...]).
    Clauses are found strictly in sequence -- A., then B., then C. --
    which avoids false splits on things like "C.U." or the Roman-numeral
    sub-items ("I. Chairperson II. ...") that sit inside a clause.
    """
    positions = []
    search_from = 0
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        match = re.compile(rf"(?:^|\s){letter}\.\s").search(body, search_from)
        if not match:
            break
        start = match.start() + (0 if body[match.start()] == letter else 1)
        positions.append((letter, start))
        search_from = start + 2
    if not positions:
        return body, []

    stem = body[: positions[0][1]].strip()
    clauses = []
    for i, (letter, start) in enumerate(positions):
        end = positions[i + 1][1] if i + 1 < len(positions) else len(body)
        clauses.append((letter, body[start + 2:end].strip()))
    return stem, clauses


def build_chunks(pages):
    articles, headings = parse_toc(pages)
    body_pages = [p for i, p in enumerate(pages) if i > 0 and not _is_toc_page(p)]  # page 1 is the cover

    chunks = []  # (citation, content)

    # Definitions list -- the first non-cover, non-TOC page.
    definitions = body_pages[0]
    chunks.append((f"{CITE}, Abbreviations and Definitions", definitions))

    text = " ".join(body_pages[1:])

    # Preamble and Supremacy Clause come before Chapter One.
    first_article = text.index("Article 1:")
    preamble_part = _strip_trailing_headings(text[:first_article].strip(), headings)
    preamble, _, supremacy = preamble_part.partition("SUPREMACY CLAUSE")
    chunks.append((f"{CITE}, Preamble", preamble.strip()))
    chunks.append((f"{CITE}, Supremacy Clause", f"Supremacy Clause. {supremacy.strip()}"))

    # Articles -- found strictly in sequence (Article 1:, Article 2:, ...), so an
    # in-text reference like "subject to Article 28" is never mistaken for a start.
    starts = []
    search_from = 0
    for number in sorted(articles):
        match = re.compile(rf"Article {number}:\s*").search(text, search_from)
        if not match:
            raise ValueError(f"Article {number} is in the table of contents but not found in the body")
        starts.append((number, match.start(), match.end()))
        search_from = match.end()

    for i, (number, _start, body_start) in enumerate(starts):
        body_end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        title, _chapter = articles[number]
        body = _strip_title(_strip_trailing_headings(text[body_start:body_end].strip(), headings), title)
        heading = f"Article {number}: {title}."  # short on purpose -- see module docstring
        cite_title = f"{title}"

        stem, clauses = split_clauses(body)

        if number == DOCTRINAL_BASIS_ARTICLE and clauses:
            # Each tenet alone, WITHOUT the shared opening sentence -- that
            # repeated stem is what made all 11 look alike (see docstring).
            for letter, clause in clauses:
                chunks.append((
                    f"{CITE}, Art. {number}({letter}): {cite_title}",
                    f"{heading} ({letter}) {clause}",
                ))
            continue

        if _words(heading) + _words(body) <= MAX_CHUNK_WORDS or not clauses:
            chunks.append((f"{CITE}, Art. {number}: {cite_title}", f"{heading} {body}"))
            continue

        # Long article: pack whole clauses into parts, each under MAX_CHUNK_WORDS.
        prefix = f"{heading} {stem}".strip()
        group = []
        for letter, clause in clauses:
            candidate = group + [(letter, clause)]
            size = _words(prefix) + sum(_words(c) + 1 for _, c in candidate)
            if group and size > MAX_CHUNK_WORDS:
                chunks.append(_part(number, cite_title, prefix, group))
                group = [(letter, clause)]
            else:
                group = candidate
        chunks.append(_part(number, cite_title, prefix, group))

    return chunks


def _part(number, cite_title, prefix, group):
    first, last = group[0][0], group[-1][0]
    span = first if first == last else f"{first}-{last}"
    content = prefix + " " + " ".join(f"({letter}) {clause}" for letter, clause in group)
    return (f"{CITE}, Art. {number}({span}): {cite_title}", content)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="show the chunks without embedding or storing them")
    parser.add_argument("--show", type=int, default=0, help="with --dry-run, print the full text of the first N chunks")
    args = parser.parse_args()

    chunks = build_chunks(read_pages())
    sizes = [_words(content) for _, content in chunks]
    print(f"{len(chunks)} chunks | words per chunk: min {min(sizes)}, max {max(sizes)}, avg {sum(sizes) // len(sizes)}")

    if args.dry_run:
        for citation, content in chunks:
            print(f"  {_words(content):>4} words  {citation}")
        for citation, content in chunks[: args.show]:
            print(f"\n--- {citation} ---\n{content}")
        return

    from app.models.rag import init_rag_tables, replace_document
    from app.services.embeddings import embed_passages

    init_rag_tables()
    embeddings = embed_passages([content for _, content in chunks])
    document_id = replace_document(
        TITLE, TIER, SOURCE, datetime.now(timezone.utc).isoformat(),
        [(citation, content, vector) for (citation, content), vector in zip(chunks, embeddings)],
    )
    print(f"Stored '{TITLE}' (document id {document_id}) with {len(chunks)} embedded chunks.")


if __name__ == "__main__":
    main()
