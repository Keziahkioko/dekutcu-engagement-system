"""
scripts/ingest_bible.py

Loads the Berean Standard Bible (public domain since 30 April 2023 -- berean.bible/terms.htm) for the RAG
companion, in two forms (Keziah, 2026-10-12):
  1. every verse into bible_verses -- for LOOKING UP a passage a member names ("Explain Romans 12:2");
  2. ~6,000 short passages (up to 5 verses, never across chapters) embedded for MEANING-search ("What does the
     Bible say about anxiety?"), as rag_chunks under one document, tier 'bible'.

The embedding takes about a million tokens -- roughly two days of Cloudflare's free daily allowance -- so it is
RESUMABLE: vectors are saved to corpus/bsb_embeddings.jsonl as they arrive; when the allowance runs out the
script stops, and the next run carries on where it left off. Nothing is loaded into rag_chunks until every
passage has its vector, then all in one transaction (rag.replace_document).

    python scripts/ingest_bible.py --dry-run     # parse and show the passages only
    python scripts/ingest_bible.py --verses      # load the verse table (lookup) -- no embedding
    python scripts/ingest_bible.py --embed       # embed what's missing (re-run until it says complete)
    python scripts/ingest_bible.py --load        # store the embedded passages for meaning-search

The text file is corpus/bsb.txt from https://bereanbible.com/bsb.txt (git-ignored, like the whole corpus).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

TEXT_PATH = os.path.join(PROJECT_ROOT, "corpus", "bsb.txt")
CACHE_PATH = os.path.join(PROJECT_ROOT, "corpus", "bsb_embeddings.jsonl")
TITLE = "Berean Standard Bible (BSB)"
TIER = "bible"
SOURCE = "corpus/bsb.txt from bereanbible.com -- public domain since 30 April 2023 (berean.bible/terms.htm)"
VERSES_PER_PASSAGE = 5
BATCH = 50


def read_verses():
    """[(book, chapter, verse, text), ...] in Bible order. Skips the 16 verses the BSB leaves empty (e.g. Matthew
    17:21 -- not in the oldest manuscripts)."""
    rows = []
    for line in open(TEXT_PATH, encoding="utf-8-sig").read().splitlines()[3:]:
        m = re.match(r"^(.+?) (\d+):(\d+)\t(.*)$", line)
        if not m:
            raise ValueError(f"unexpected line in {TEXT_PATH}: {line[:80]!r}")
        text = m.group(4).strip()
        if text:
            rows.append((m.group(1), int(m.group(2)), int(m.group(3)), text))
    return rows


def build_passages(rows):
    """[(citation, content), ...]: up to 5 verses, within one chapter; a short tail (1-2 verses) joins the
    passage before it. Content starts with the reference and numbers each verse, so a passage reads on its own."""
    chapters = {}
    for book, chapter, verse, text in rows:
        chapters.setdefault((book, chapter), []).append((verse, text))
    passages = []
    for (book, chapter), verses in chapters.items():
        groups = [verses[i:i + VERSES_PER_PASSAGE] for i in range(0, len(verses), VERSES_PER_PASSAGE)]
        if len(groups) > 1 and len(groups[-1]) < 3:
            tail = groups.pop()
            groups[-1] = groups[-1] + tail
        for g in groups:
            first, last = g[0][0], g[-1][0]
            ref = f"{book} {chapter}:{first}" + (f"-{last}" if last != first else "")
            content = f"{ref}. " + " ".join(f"{v} {t}" for v, t in g)
            passages.append((f"{ref} (BSB)", content))
    return passages


def _cached():
    vectors = {}
    if os.path.exists(CACHE_PATH):
        for line in open(CACHE_PATH, encoding="utf-8"):
            if line.strip():
                item = json.loads(line)
                vectors[item["citation"]] = item["embedding"]
    return vectors


def embed_missing(passages):
    from app.services.embeddings import embed_passages
    done = _cached()
    todo = [(c, t) for c, t in passages if c not in done]
    print(f"{len(done)} of {len(passages)} passages already embedded; {len(todo)} to go.")
    with open(CACHE_PATH, "a", encoding="utf-8") as out:
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            try:
                vectors = embed_passages([t for _, t in batch])
            except Exception as e:
                print(f"Stopped after {len(done) + start} of {len(passages)}: {e}\n"
                      "(Probably Cloudflare's daily free allowance -- run --embed again tomorrow; nothing is lost.)")
                return False
            for (citation, _), vector in zip(batch, vectors):
                out.write(json.dumps({"citation": citation, "embedding": vector}) + "\n")
            out.flush()
            if (start // BATCH) % 20 == 0:
                print(f"  ... {len(done) + start + len(batch)} of {len(passages)}")
    print("Complete -- every passage is embedded. Next: --load")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verses", action="store_true", help="load the verse table (for lookup)")
    parser.add_argument("--embed", action="store_true", help="embed the passages not yet embedded")
    parser.add_argument("--load", action="store_true", help="store the embedded passages for meaning-search")
    args = parser.parse_args()

    rows = read_verses()
    passages = build_passages(rows)
    words = [len(c.split()) for _, c in passages]
    print(f"{len(rows)} verses, {len(passages)} passages | words per passage: min {min(words)}, max {max(words)}, "
          f"avg {sum(words) // len(words)}, total {sum(words)}")

    if args.dry_run:
        for citation, content in passages[:3] + passages[-2:]:
            print(f"\n--- {citation} ---\n{content}")
        return
    if args.verses:
        from app.models.bible import init_bible_table, replace_all_verses
        init_bible_table()
        replace_all_verses(rows)
        print(f"Stored {len(rows)} verses in bible_verses.")
    if args.embed:
        embed_missing(passages)
    if args.load:
        vectors = _cached()
        missing = [c for c, _ in passages if c not in vectors]
        if missing:
            sys.exit(f"Not loading: {len(missing)} passages have no embedding yet (e.g. {missing[0]}). Run --embed.")
        from app.models.rag import init_rag_tables, replace_document
        init_rag_tables()
        document_id = replace_document(TITLE, TIER, SOURCE, datetime.now(timezone.utc).isoformat(),
                                       [(c, t, vectors[c]) for c, t in passages])
        print(f"Stored '{TITLE}' (document id {document_id}) with {len(passages)} embedded passages.")


if __name__ == "__main__":
    main()
