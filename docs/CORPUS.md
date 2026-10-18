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
- **bible** — Scripture (the Berean Standard Bible). Quoted and explained in
  context, cited under "Scripture:"; the constitution stays the authority on what
  DeKUTCU believes, and secondary issues are still not taken sides on. Searched
  separately from DeKUTCU's own materials so it can't crowd them out; a reference
  a member names is looked up exactly (`bible_verses`). Added 2026-10-12.
- **dekutcu** — DeKUTCU's own documents other than the constitution (the Code of
  Conduct; later the secondary issues guide and a practical FAQ). Labelled "DeKUTCU's
  own", searched with the constitution. The constitution prevails where they differ
  (Code of Conduct, Art. 3).

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
| Berean Standard Bible (BSB) | bible | https://bereanbible.com/bsb.txt -- public domain since 30 April 2023 (berean.bible/terms.htm). File: `corpus/bsb.txt`. All 31,086 verses in `bible_verses` (exact lookup; the 16 verses the BSB leaves empty are skipped); 6,219 passages of up to 5 verses embedded for meaning-search | `scripts/ingest_bible.py` | 6,219 (once `--load` has run) |
| DeKUTCU Code of Conduct (2026) | dekutcu | DeKUTCU's own Code of Conduct, ratified at the 2025/2026 AGM (Art. 28; ratification confirmed by Keziah 2026-10-14). File: `corpus/DeKUTCU CODE OF CONDUCT.pdf` | `scripts/ingest_code_of_conduct.py` | 30 |
| DeKUTCU guide to secondary issues (2026) | dekutcu | `docs/secondary_issues.md` -- drafted 2026-10-12 from research on theological triage and how IFES/UCCF Christian Unions handle secondary matters; approved for DeKUTCU by Keziah Kioko 2026-10-15. 11 secondary issues + 5 Christian living questions; DeKUTCU's own standard stated where the Constitution or Code of Conduct sets one | `scripts/ingest_secondary_issues.py` | 17 |
| Thomas Watson, A Body of Divinity (1692) | supporting | CCEL (ccel.org/ccel/watson/divinity), "Rights: Public Domain". Covers Art. 11(A)-(I). Left out: Spurgeon's memoir, the Perseverance chapter, passages arguing election (`docs/corpus_exclusions/watson.md`). Approved 2026-10-15 | `scripts/ingest_book.py watson` | 672 |
| John Owen, Of the Mortification of Sin in Believers (1656) | supporting | CCEL (ccel.org/ccel/owen/mort), Goold edition -- public domain. Art. 11(H). Polemic passages left out (`owen.md`). Approved 2026-10-15 | `scripts/ingest_book.py owen` | 175 |
| C. H. Spurgeon, All of Grace (1886) | supporting | CCEL (ccel.org/ccel/spurgeon/grace) -- public domain. Art. 11(D), (F), (G). Perseverance chapters and election passages left out (`spurgeon.md`). Approved 2026-10-15 | `scripts/ingest_book.py spurgeon` | 125 |
| J. C. Ryle, Holiness (1879) | supporting | CCEL (ccel.org/ccel/ryle/holiness), 1952 reprint of Ryle's text -- public domain. Art. 11(H), (J). "Wants of the Times" and Church-of-England party passages left out (`ryle.md`). Approved 2026-10-15; party passages 2026-10-18 | `scripts/ingest_book.py ryle` | 672 |
| Jeremiah Burroughs, Irenicum (1653) | supporting | EEBO-TCP A30587 (github.com/textcreationpartnership/A30587) -- Creative Commons 0. Converted by `scripts/convert_tcp.py` (old letter forms only, spelling as written). Art. 11(K), unity. Toleration-debate and church-government chapters left out (`irenicum.md`). Approved 2026-10-18 | `scripts/ingest_book.py irenicum` | 435 |
| Matthew Henry, Commentary (1706-1714), selected chapters | supporting | CCEL (ccel.org/ccel/henry/mhc5, mhc6) -- public domain. 21 chapters (Matt 24-25, John 17, Acts 1 & 10, Rom 14-15, 1 Cor 12, 2 Cor 5, Gal 3, Eph 2 & 4, Phil 3, Col 3, 1 Thess 4-5, 2 Thess 1, Titus 2, James 2, 2 Pet 3, 1 John 3), converted by `scripts/convert_henry.py`. Art. 11(K) unity without discrimination, (I) second coming, (B) final judgment, (J) the Church. Off-gap sections and secondary issues left out (`henry.md`). Approved 2026-10-18 | `scripts/ingest_book.py henry` | 534 |
