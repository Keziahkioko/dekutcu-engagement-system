"""
scripts/ingest_secondary_issues.py

Loads docs/secondary_issues.md -- DeKUTCU's guide to fundamental truths and secondary issues, approved by Keziah
for DeKUTCU on 2026-10-15 -- into the RAG companion as DeKUTCU's own material (tier 'dekutcu'), so a question on
a secondary issue gets what the Bible says, the main views described fairly, and "DeKUTCU does not take a
position", instead of a refusal.

One chunk per issue ("### Baptism" ...), cited "DeKUTCU guide to secondary issues: Baptism", each opening with
what kind of question it is, so the model always knows how to treat it. Also the guide's opening ("Why this guide
exists"). Skipped: the list of fundamental truths (it restates Article 11 -- the constitution itself carries that
authority) and "How the companion uses this guide" (instructions, not content).

    python scripts/ingest_secondary_issues.py --dry-run
    python scripts/ingest_secondary_issues.py
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

GUIDE_PATH = os.path.join(PROJECT_ROOT, "docs", "secondary_issues.md")
TITLE = "DeKUTCU guide to secondary issues (2026)"
TIER = "dekutcu"
SOURCE = "docs/secondary_issues.md -- drafted 2026-10-12, approved for DeKUTCU by Keziah Kioko 2026-10-15"
CITE = "DeKUTCU guide to secondary issues"

_KIND = {
    "Secondary issues": "A secondary issue - DeKUTCU does not take a position on it.",
    "Christian living questions": "A Christian living question - the Bible's principles, how churches differ, and "
                                  "DeKUTCU's own standard where it has one.",
}


def _plain(text):
    """Markdown to plain sentences: no bold/italic markers, list dashes become line breaks."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^\s*-\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def build_chunks():
    text = open(GUIDE_PATH, encoding="utf-8").read()
    chunks = []
    sections = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    for section in sections:
        heading, _, body = section.partition("\n")
        heading = heading.strip()
        if heading.startswith("Why this guide exists"):
            chunks.append((f"{CITE}: Why DeKUTCU does not take sides on secondary issues",
                           f"{CITE}: why DeKUTCU does not take sides on secondary issues. {_plain(body)}"))
            continue
        kind = next((v for k, v in _KIND.items() if heading.startswith(k)), None)
        if kind is None:
            continue          # the fundamental truths list, and the companion's own instructions
        for issue in re.split(r"^### ", body, flags=re.MULTILINE)[1:]:
            name, _, content = issue.partition("\n")
            name = name.strip()
            chunks.append((f"{CITE}: {name}", f"{CITE}: {name}. {kind} {_plain(content)}"))
    return chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show", type=int, default=0)
    args = parser.parse_args()
    chunks = build_chunks()
    sizes = [len(c.split()) for _, c in chunks]
    print(f"{len(chunks)} chunks | words per chunk: min {min(sizes)}, max {max(sizes)}, avg {sum(sizes) // len(sizes)}")
    if args.dry_run:
        for (citation, _), n in zip(chunks, sizes):
            print(f"  {n:>4} words  {citation}")
        for citation, content in chunks[: args.show]:
            print(f"\n--- {citation} ---\n{content}")
        return
    from app.models.rag import init_rag_tables, replace_document
    from app.services.embeddings import embed_passages
    init_rag_tables()
    vectors = embed_passages([content for _, content in chunks])
    document_id = replace_document(TITLE, TIER, SOURCE, datetime.now(timezone.utc).isoformat(),
                                   [(c, t, v) for (c, t), v in zip(chunks, vectors)])
    print(f"Stored '{TITLE}' (document id {document_id}) with {len(chunks)} embedded chunks.")


if __name__ == "__main__":
    main()
