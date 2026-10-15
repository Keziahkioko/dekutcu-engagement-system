"""
scripts/ingest_book.py

Loads a vetted, public-domain Christian book into the RAG companion as a SUPPORTING source (Keziah, 2026-10-15):
always labelled "Supporting source (not DeKUTCU's official position)", searched separately from DeKUTCU's own
documents so it can never crowd them out, and -- as agreed -- with the parts that argue one side of a secondary
issue left out, because DeKUTCU's approved guide presents every side fairly and a one-sided chapter would tilt
the answer. Everything left out is written to docs/corpus_exclusions/<book>.md for review: nothing disappears
silently.

Each book is a small configuration in BOOKS below. The text is split by chapter, then into passages of about
200 words (whole paragraphs), cited "Thomas Watson, A Body of Divinity: The Trinity".

    python scripts/ingest_book.py watson --dry-run     # chapters, passages, and what would be left out
    python scripts/ingest_book.py watson               # embed + store (re-running replaces the book atomically)

Embedding goes through Cloudflare's free allowance, which the live companion also uses: bulk loading stops at
40% of the day's allowance (app/services/embeddings.py keeps a daily ledger -- the whole allowance was used up
twice before), saving its progress; run the same command the next day to carry on.
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

TARGET_WORDS = 200

# Passages that argue election/predestination -- one view of a secondary issue (the guide gives all views).
_ELECTION = re.compile(r"\b(elect|elected|election|electing|predestinat\w*)\b", re.IGNORECASE)

BOOKS = {
    "watson": {
        "file": "corpus/watson_body_of_divinity.txt",
        "title": "Thomas Watson, A Body of Divinity (1692)",
        "cite": "Thomas Watson, A Body of Divinity",
        "source": "CCEL (ccel.org/ccel/watson/divinity) -- Rights: Public Domain. Approved by Keziah 2026-10-15.",
        # The teaching starts here; before it is C. H. Spurgeon's memoir of Watson (biography, with a remark against
        # the Church of England out of place in a non-denominational CU -- left out, Keziah approved).
        "start": "1. A Preliminary Discourse To Catechising",
        # The Reformed view of one secondary issue (the guide presents it as one of three positions).
        "skip_chapters": {"Perseverance"},
        "skip_passages": _ELECTION,
    },
}

_HEADING = re.compile(r"^ {0,2}(\d+)\. ([A-Z][A-Za-z',’ -]{2,70})$")


def _squash(text):
    return re.sub(r"\s+", " ", text).strip()


def read_chapters(book):
    """[(chapter title, [paragraph, ...]), ...] from the start marker on; CCEL's section rules dropped."""
    text = open(os.path.join(PROJECT_ROOT, book["file"]), encoding="utf-8", errors="replace").read()
    text = text[text.index(book["start"]):]
    chapters, title, paragraph, paragraphs = [], None, [], []

    def end_paragraph():
        if paragraph:
            paragraphs.append(_squash(" ".join(paragraph)))
            paragraph.clear()

    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            end_paragraph()
            if title and paragraphs:
                chapters.append((title, list(paragraphs)))
            title, paragraphs = heading.group(2).strip(), []
            continue
        if not line.strip() or set(line.strip()) <= {"_"}:
            end_paragraph()
        else:
            paragraph.append(line)
    end_paragraph()
    if title and paragraphs:
        chapters.append((title, paragraphs))
    return chapters


def build(book):
    """(passages [(citation, content)], left_out [(chapter, reason, text)])."""
    passages, left_out = [], []
    for title, paragraphs in read_chapters(book):
        if title in book["skip_chapters"]:
            left_out.append((title, "whole chapter", " ".join(paragraphs)))
            continue
        pieces = []
        for p in paragraphs:
            if book["skip_passages"] and book["skip_passages"].search(p):
                left_out.append((title, "argues election/predestination", p))
            else:
                pieces.extend(_split_long(p))
        for body in _pack(pieces):
            passages.append((f"{book['cite']}: {title}", f"{book['cite']}, {title}. {body}"))
    return passages, left_out


MAX_WORDS = 320   # the embedding model reads ~512 tokens; longer passages would be cut off unseen


def _split_long(paragraph):
    """A paragraph longer than MAX_WORDS becomes several, split at sentence ends."""
    if len(paragraph.split()) <= MAX_WORDS:
        return [paragraph]
    parts, current = [], []
    for sentence in re.split(r"(?<=[.!?;])\s+", paragraph):
        if current and len(" ".join(current + [sentence]).split()) > TARGET_WORDS:
            parts.append(" ".join(current))
            current = []
        current.append(sentence)
    if current:
        parts.append(" ".join(current))
    return parts


def _pack(pieces):
    """Consecutive pieces joined into passages of about TARGET_WORDS (never over MAX_WORDS); a short tail joins the last."""
    passages, current = [], []
    for piece in pieces:
        if current and len(" ".join(current + [piece]).split()) > MAX_WORDS:
            passages.append(" ".join(current))
            current = []
        current.append(piece)
        if len(" ".join(current).split()) >= TARGET_WORDS:
            passages.append(" ".join(current))
            current = []
    if current:
        tail = " ".join(current)
        if passages and len(tail.split()) < TARGET_WORDS // 3 and len((passages[-1] + " " + tail).split()) <= MAX_WORDS:
            passages[-1] += " " + tail
        else:
            passages.append(tail)
    return passages


def _embeddings(key, passages):
    """
    Vectors for these exact passages -- reused from corpus/<book>_embeddings.json when the passages haven't changed,
    so loading a book into a second database (real + demo) doesn't spend Cloudflare's allowance twice.
    """
    import hashlib
    import json
    from app.services.embeddings import embed_passages, DailyBudgetReached
    cache_path = os.path.join(PROJECT_ROOT, "corpus", f"{key}_embeddings.json")
    fingerprint = hashlib.sha256("\n".join(c for _, c in passages).encode("utf-8")).hexdigest()
    vectors = [None] * len(passages)
    if os.path.exists(cache_path):
        cached = json.load(open(cache_path, encoding="utf-8"))
        if cached.get("fingerprint") == fingerprint:
            vectors = cached["vectors"]
    missing = [i for i, v in enumerate(vectors) if v is None]
    if not missing:
        print("Reusing the saved embeddings (passages unchanged).")
        return vectors
    # RESUMABLE (2026-10-15): a big book needs more than one day's bulk budget, so embed batch by batch, saving
    # as we go; when the day's budget is reached, stop -- the next run carries on.
    print(f"{len(passages) - len(missing)} of {len(passages)} passages already embedded; embedding the rest.")
    try:
        for start in range(0, len(missing), 50):
            batch = missing[start:start + 50]
            for i, vector in zip(batch, embed_passages([passages[i][1] for i in batch])):
                vectors[i] = vector
            json.dump({"fingerprint": fingerprint, "vectors": vectors}, open(cache_path, "w", encoding="utf-8"))
    except DailyBudgetReached as e:
        done = sum(v is not None for v in vectors)
        sys.exit(f"Stopped at {done} of {len(passages)} passages -- {e}\nNothing is lost: run the same command again tomorrow.")
    return vectors


def write_exclusions(key, book, left_out):
    folder = os.path.join(PROJECT_ROOT, "docs", "corpus_exclusions")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{key}.md")
    with open(path, "w", encoding="utf-8") as out:
        out.write(f"# Left out of {book['title']}\n\n"
                  "Approved policy (Keziah, 2026-10-15): passages that argue one side of a secondary issue are left out --\n"
                  "DeKUTCU's guide to secondary issues presents every side fairly. Listed here so nothing is dropped silently.\n\n")
        for chapter, reason, text in left_out:
            preview = text if len(text) < 400 else text[:400] + " ..."
            out.write(f"- **{chapter}** ({reason}, {len(text.split())} words): {preview}\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("book", choices=sorted(BOOKS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    book = BOOKS[args.book]

    chapters = read_chapters(book)
    passages, left_out = build(book)
    words = [len(c.split()) for _, c in passages]
    dropped = sum(len(t.split()) for _, _, t in left_out)
    print(f"{book['title']}: {len(chapters)} chapters -> {len(passages)} passages "
          f"(words min {min(words)}, max {max(words)}, avg {sum(words) // len(words)}); "
          f"left out: {len(left_out)} items, {dropped} words")
    path = write_exclusions(args.book, book, left_out)
    print(f"Left-out list: {os.path.relpath(path, PROJECT_ROOT)}")
    if args.dry_run:
        for title, paragraphs in chapters:
            print(f"  {title} ({sum(len(p.split()) for p in paragraphs)} words)")
        return
    from app.models.rag import init_rag_tables, replace_document
    init_rag_tables()
    vectors = _embeddings(args.book, passages)
    document_id = replace_document(book["title"], "supporting", book["source"], datetime.now(timezone.utc).isoformat(),
                                   [(c, t, v) for (c, t), v in zip(passages, vectors)])
    print(f"Stored '{book['title']}' (document id {document_id}) with {len(passages)} embedded passages.")


if __name__ == "__main__":
    main()
