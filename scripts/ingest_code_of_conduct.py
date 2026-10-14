"""
scripts/ingest_code_of_conduct.py

Loads the DeKUTCU Code of Conduct (ratified at the 2025/2026 AGM -- confirmed by Keziah, 2026-10-14) into the RAG
companion as DeKUTCU's own document (tier 'dekutcu'), next to the constitution. Same approach as
ingest_constitution.py: one chunk per article, cited "DeKUTCU Code of Conduct, Art. 9: For Ladies ministering";
the preamble and the definitions as their own chunks; cover and table of contents skipped (they'd only produce
misleading matches). Every chunk starts with a short heading so it reads on its own.

    python scripts/ingest_code_of_conduct.py --dry-run   # show the chunks only
    python scripts/ingest_code_of_conduct.py             # embed + store (re-running replaces it atomically)
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

from pypdf import PdfReader

PDF_PATH = os.path.join(PROJECT_ROOT, "corpus", "DeKUTCU CODE OF CONDUCT.pdf")
TITLE = "DeKUTCU Code of Conduct (2026)"
TIER = "dekutcu"
SOURCE = "corpus/DeKUTCU CODE OF CONDUCT.pdf -- DeKUTCU's own Code of Conduct, ratified at the 2025/2026 AGM (Art. 28)"
CITE = "DeKUTCU Code of Conduct"

_ARTICLE = re.compile(r"Article (\d+): (.+?)(?=\s{2,}|\.\s|:\s|\n)")


def _clean(text):
    text = re.sub(r"DeKUTCU Code of Conduct", " ", text)
    text = re.sub(r"Page \| \d+", " ", text)
    text = re.sub(r"CHAPTER [A-Z]+\s*\n[A-Z ,'\-]+\n", " ", text)     # chapter headings belong to no single article
    return text


def read_text():
    """Pages 2 (definitions) and 5 onwards; pages 1, 3-4 are the cover and table of contents."""
    pages = PdfReader(PDF_PATH).pages
    definitions = _clean(pages[1].extract_text())
    body = "\n".join(_clean(p.extract_text()) for p in pages[4:])
    return definitions, body


def _squash(text):
    return re.sub(r"\s+", " ", text).strip()


def read_titles():
    """{article number: title} from the code's OWN table of contents (pages 3-4), so a heading that wraps onto
    two lines in the body ("...Dockets'" / "Policy") still gets its full title -- as ingest_constitution.py does."""
    pages = PdfReader(PDF_PATH).pages
    toc = _squash(" ".join(pages[i].extract_text() for i in (2, 3)))
    return {int(n): t.strip() for n, t in re.findall(r"Article (\d+): (.+?)\s*\.{3,}\s*\d+", toc)}


def build_chunks(definitions, body):
    chunks = [(f"{CITE}, Definitions", "Code of Conduct: Abbreviations and definition of terms. "
               + _squash(definitions.split("as defined:", 1)[-1]))]
    first = body.find("Article 1:")
    preamble = _squash(body[:first].replace("PREAMBLE", ""))
    chunks.append((f"{CITE}, Preamble", f"Code of Conduct: Preamble. {preamble}"))
    titles = read_titles()
    starts = [m for m in re.finditer(r"Article (\d+): ", body)]
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(body)
        number = int(m.group(1))
        title = titles[number].rstrip(".:")
        # The body repeats the title (perhaps wrapped onto two lines) before the text -- drop it.
        rest = _squash(body[m.end():end])
        content = rest[len(titles[number]):].lstrip(" .:") if rest.startswith(titles[number]) else rest
        chunks.append((f"{CITE}, Art. {number}: {title}", f"Code of Conduct, Article {number}: {title}. {content}"))
    return chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show", type=int, default=0)
    args = parser.parse_args()

    chunks = build_chunks(*read_text())
    sizes = [len(c.split()) for _, c in chunks]
    print(f"{len(chunks)} chunks | words per chunk: min {min(sizes)}, max {max(sizes)}, avg {sum(sizes) // len(sizes)}")
    if args.dry_run:
        for (citation, content), n in zip(chunks, sizes):
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
