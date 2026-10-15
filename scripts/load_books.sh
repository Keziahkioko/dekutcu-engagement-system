#!/usr/bin/env bash
# Loads every prepared supporting book into the real database and the demo copy (scripts/ingest_book.py).
# Resumable: if Cloudflare's daily budget runs out part-way, run it again the next day (after 3am Kenya time) and it
# carries on; books already complete are reloaded from their saved vectors at no cost.
#   bash scripts/load_books.sh
cd "$(dirname "$0")/.."
PY=venv/Scripts/python.exe; [ -x "$PY" ] || PY=python
for book in watson owen spurgeon ryle; do
  echo "== $book (real database)"
  PYTHONIOENCODING=utf-8 "$PY" scripts/ingest_book.py "$book" 2>&1 | grep -v "HTTP Request" || true
  if [ -f "corpus/${book}_embeddings.json" ]; then
    echo "== $book (demo copy)"
    PYTHONIOENCODING=utf-8 "$PY" -c "
import os, sys, runpy
from demo.demo_safety import demo_database_url
os.environ['DATABASE_URL'] = demo_database_url()
sys.argv = ['ingest_book.py', '$book']
runpy.run_path('scripts/ingest_book.py', run_name='__main__')" 2>&1 | grep -v "HTTP Request" || true
  fi
done
