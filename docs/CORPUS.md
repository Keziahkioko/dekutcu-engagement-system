# RAG Companion corpus

The documents the Stage 12 RAG companion is allowed to answer from. The
files themselves live in the git-ignored `corpus/` folder and in the
database (`rag_documents` / `rag_chunks`); this list is the committed
record of *what* is in the corpus, its tier, and where it came from.
See `docs/PROJECT_LOG.md`, Stage 12 decision 1, for the reasoning.

## Tiers

- **constitution** — DeKUTCU's own voice and the sole authority. The bot
  never contradicts its doctrinal basis (Art. 11).
- **supporting** — a vetted outside source. Always labelled as supporting
  in answers, never presented as DeKUTCU's official position.

## Vetting criteria for any supporting source

Every supporting document must meet all three, and be signed off by
Keziah or the leadership before it is loaded:

1. It agrees with the constitution's doctrinal basis (Art. 11).
2. It is silent on, or clearly not taking sides on, secondary issues
   (mode of baptism, church government, spiritual gifts, etc.) —
   DeKUTCU is non-denominational (Preamble, clause 4).
3. Its copyright/terms allow it to be stored and quoted.

Likely first candidate: FOCUS Kenya's own materials — DeKUTCU is a
member of FOCUS Kenya (Art. 4) and operates within its spiritual
doctrine (Art. 41(K)).

## Documents

| Document | Tier | Source | Loaded by | Chunks |
|---|---|---|---|---|
| DeKUTCU Constitution (2024) | constitution | DeKUTCU's own constitution, adopted at the 2024 AGM (Art. 74). File: `corpus/DeKUTCU CONSTITUTION.pdf` | `scripts/ingest_constitution.py` | 95 |
