"""
app/services/rag_companion.py

Stage 12 (Objective 4): answers members' questions from DeKUTCU's own
materials -- currently the constitution -- with citations. Every design
choice here was settled with Keziah one decision at a time; see
PROJECT_LOG.md, Stage 12, for the reasoning and the retrieval experiments.

The path a question takes:
  1. Safety first: the same escalation.assess_severity used everywhere
     (Stage 11). Acute risk -> leader notified regardless of consent,
     always transparently, and nothing is retrieved or generated.
     Distress -> the question IS still answered, but followed by the
     Stage 11 consent question ("Would it be okay if I let a leader
     know?") rather than the casual offer.
  2. Retrieval: the 8 chunks nearest in meaning (Cloudflare
     bge-large-en-v1.5 + pgvector). 8, not 3: measured on the real
     constitution, the right passage was present for 11/12 test
     questions at 8 vs 9/12 at 3.
  3. Generation: openai/gpt-oss-120b (its own Groq daily allowance, so
     RAG can't drain the classifier's). It returns a small JSON verdict:
     is the question covered, is it a secondary issue, the answer, and
     WHICH NUMBERED PASSAGES it used.
  4. Citations are written by this code, never by the model: the model
     only names passage numbers; any number that isn't one of the 8 is
     discarded and counted (invalid_citations). So a citation to an
     article that doesn't exist can't reach a member. If the model says
     "covered" but cites nothing valid, the answer is treated as NOT
     covered -- an answer that can't point to its source isn't grounded.
  5. Not covered / secondary issue / pastoral question -> the soft,
     consent-first leader OFFER (escalation.start_leader_offer).
  6. Every question is logged (rag_queries) for the evaluation.

"Not covered" is decided by the model reading the passages, NOT by a
similarity threshold: on the real constitution no cut-off separated
covered from uncovered questions (the uncovered infant-baptism question
scored above several answerable ones).

Known limitation, accepted for now: each question is answered on its
own -- a follow-up like "what about the second one?" doesn't carry the
previous answer's context.
"""

import re
import json
from datetime import datetime, timezone

from app.models.rag import nearest_chunks, chunks_with_citation_prefix, log_query
from app.services.embeddings import embed_query
from app.services.llm_client import create_chat_completion
from app.services import escalation

RAG_MODEL = "openai/gpt-oss-120b"
TOP_K = 8

# Small-to-big retrieval, for the doctrinal basis only: it's searched as
# 11 separate clauses (precise matching), but if ANY clause is retrieved
# the model gets ALL of Art. 11 (~150 words). Doctrine is answered from
# the whole statement of faith, never a fragment -- the clauses interpret
# each other (11(D) redemption and 11(F) justification belong together).
# Found by testing: "How do I get to heaven?" retrieved 11(D) but not
# 11(F), and the model filled the gap with membership passages about
# being "born again" -- a salvation claim the constitution never makes.
DOCTRINAL_BASIS_PREFIX = "DeKUTCU Constitution, Art. 11("

NOT_COVERED_TEXT = (
    "That isn't something DeKUTCU's materials that I can draw on cover, so I'd "
    "rather not guess."
)
SECONDARY_ISSUE_TEXT = (
    "DeKUTCU is a non-denominational fellowship of believers from different "
    "churches, so it doesn't take a position on this. It's a great question to "
    "explore with a leader or with your own church."
)
ERROR_TEXT = "Sorry, I couldn't look that up right now -- please try again in a little while."

_RULES = """You are the WhatsApp assistant of DeKUTCU (Dedan Kimathi University of Technology Christian Union). Answer the member's question using ONLY the numbered passages from DeKUTCU's materials below.

Rules:
1. Every statement in your answer must be directly supported by a passage. Do not add facts, teaching, examples, conclusions or assurances the passages do not state. Quote a Bible verse ONLY if that verse appears in a passage -- never add scripture from memory.
2. Do not combine passages that are about different things into a claim none of them makes. Rules about membership, discipline, leadership or governance describe the Christian Union as an ORGANISATION -- never present them as spiritual teaching. For example, the membership declaration (what a student affirms when joining) is not a statement of how a person is saved, and the rules on restoring a suspended member are not teaching about personal repentance.
3. Set "covered" to false if the passages do not actually answer the question. Passages that are merely on a related topic do not count.
4. DeKUTCU is non-denominational. If the question is about an issue its doctrinal basis does not settle -- such as the mode or timing of baptism, church government, or spiritual gifts -- set "secondary_issue" to true and do not take a position.
5. For questions about salvation, base the answer on the doctrinal basis clauses about redemption and justification, and present salvation as DeKUTCU's doctrinal basis does: God's work, by His grace alone, received through faith -- never as something a person achieves by their own declaration, decision or effort.
6. Passages marked "Supporting source" are not DeKUTCU's own position; if you rely on one, say so ("a supporting source explains...").
7. Keep the answer short and warm: 2-4 sentences of plain WhatsApp text, no markdown, no headings. Never mention passage numbers, article numbers or the word "passage" in the answer -- sources are added automatically from the "sources" list.{pastoral_rule}

Respond with ONLY a JSON object:
{{"covered": true or false, "secondary_issue": true or false, "answer": "<your answer>", "sources": [<numbers of the passages you actually used>]}}"""

_PASTORAL_RULE = """
8. This is a personal question. Share what the passages teach, but NEVER tell the member what they personally should do (for example, whether to leave a church, end a relationship or take a particular decision) -- that belongs to them and a leader who knows their situation."""


def _now():
    return datetime.now(timezone.utc).isoformat()


# Everyday word -> the constitution's own term, applied ONLY to the text that
# is searched (the answering model still sees the member's original words).
# Found in the first live test: the constitution never says "election" -- it
# says "nomination" -- so "How do elections of the exec happen?" retrieved
# termination and by-nomination articles and the answer came out wrong. Unlike
# free query-rewriting by a model (tested earlier, no better), this is a short,
# fixed, hand-written list from reading THIS constitution: predictable and
# easy to extend when real members' questions reveal new gaps.
_GLOSSARY = [
    (re.compile(r"\belect(?:ion|ions|ed|ing)?\b", re.IGNORECASE), "nomination, Nomination College"),
    (re.compile(r"\bAGMs?\b", re.IGNORECASE), "Annual General Meeting, general meetings"),
    # The constitution frames voting as a RIGHT of members (Art. 14/16), not an
    # AGM procedure -- without this, "Can I vote at the AGM?" found only the AGM
    # procedure articles (tested: MISS -> #2).
    (re.compile(r"\bvot(?:e|es|ing)\b", re.IGNORECASE), "entitled to vote, members' rights"),
    (re.compile(r"\bexecs?\b", re.IGNORECASE), "Executive Committee"),
    (re.compile(r"\bchair\b", re.IGNORECASE), "chairperson"),
    (re.compile(r"\b(?:money|contributions?)\b", re.IGNORECASE), "funds, finance"),
    (re.compile(r"\b(?:heaven|saved)\b", re.IGNORECASE), "salvation, redemption, justification"),
]


def _search_text(question):
    """The question with each glossary term's constitution wording added after it in brackets."""
    text = question
    for pattern, constitution_terms in _GLOSSARY:
        text = pattern.sub(lambda m: f"{m.group(0)} ({constitution_terms})", text)
    return text


# "(Art 42)", "(Art. 11(G) and (H))", "(Article 19)" -- one level of nested
# brackets allowed, since clause letters are themselves bracketed.
_INLINE_ARTICLE_REF = re.compile(r"\s*\((?:Art\.?|Articles?)\s[^()]*(?:\([^()]*\)[^()]*)*\)")


def _strip_inline_citations(answer):
    """
    Found by testing: the model sometimes cites articles inside the
    answer text ("...counselling sessions (Art 42)") -- references the
    code-checked sources list never saw. Every citation a member sees
    must come from that checked list, so inline ones are removed here
    (the prompt also forbids them; this is the guarantee).
    """
    answer = _INLINE_ARTICLE_REF.sub("", answer)
    # Article numbers written INTO a sentence ("...as set out in Art 53, with...")
    # can't simply be deleted without breaking the grammar -- found in the first
    # live test. They're replaced with "the constitution" instead; the exact,
    # checked citation stays on the Source line.
    answer = _IN_SENTENCE_ARTICLE_REF.sub("the constitution", answer)
    return re.sub(r"\s+([.,;:])", r"\1", answer).strip()


_IN_SENTENCE_ARTICLE_REF = re.compile(
    r"\b(?:Art\.?|Articles?)\s*\d+(?:\s*\([A-Z](?:\s*-\s*[A-Z])?\))*"
    r"(?:\s*(?:,|and|&)\s*\d+(?:\s*\([A-Z](?:\s*-\s*[A-Z])?\))*)*",
    re.IGNORECASE,
)


def _format_passages(chunks):
    lines = []
    for i, chunk in enumerate(chunks, start=1):
        label = "Supporting source" if chunk["tier"] == "supporting" else "DeKUTCU's own"
        lines.append(f"[{i}] ({label}) {chunk['citation']}\n{chunk['content']}")
    return "\n\n".join(lines)


def _source_line(cited_chunks):
    own = [c["citation"] for c in cited_chunks if c["tier"] != "supporting"]
    supporting = [c["citation"] for c in cited_chunks if c["tier"] == "supporting"]
    lines = []
    if own:
        lines.append(("Source: " if len(own) == 1 else "Sources: ") + "; ".join(own))
    if supporting:
        lines.append("Supporting source (not DeKUTCU's official position): " + "; ".join(supporting))
    return "\n".join(lines)


def _expand_doctrinal_basis(chunks):
    """
    If any doctrinal-basis clause was retrieved, replace those clauses
    with the WHOLE doctrinal basis, placed where the first one ranked.
    Clauses that were retrieved keep their similarity; the ones added by
    expansion have similarity None, so the evaluation log shows exactly
    what retrieval found versus what expansion added.
    """
    if not any(c["citation"].startswith(DOCTRINAL_BASIS_PREFIX) for c in chunks):
        return chunks
    retrieved_similarity = {c["citation"]: c["similarity"] for c in chunks}
    whole = [dict(c, similarity=retrieved_similarity.get(c["citation"]))
             for c in chunks_with_citation_prefix(DOCTRINAL_BASIS_PREFIX)]
    expanded, inserted = [], False
    for c in chunks:
        if c["citation"].startswith(DOCTRINAL_BASIS_PREFIX):
            if not inserted:
                expanded.extend(whole)
                inserted = True
        else:
            expanded.append(c)
    return expanded


def _generate(question, chunks, pastoral):
    system = _RULES.format(pastoral_rule=_PASTORAL_RULE if pastoral else "")
    user = f"Passages:\n\n{_format_passages(chunks)}\n\nMember's question: {question}"
    response = create_chat_completion(
        model=RAG_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    tokens = getattr(getattr(response, "usage", None), "total_tokens", None)
    return json.loads(response.choices[0].message.content), tokens


def answer_question(member, question, pastoral=False):
    """
    The entry point from intent_router -- general_question (pastoral=False)
    or pastoral_question (pastoral=True). Returns the reply text.
    """
    kind = "pastoral" if pastoral else "general"
    whatsapp_id = member["whatsapp_id"]
    reg_number = member["reg_number"]

    def log(severity, outcome, retrieved=None, answer=None, cited=None, invalid=0, tokens=None):
        log_query(reg_number, question, kind, severity, retrieved or [], outcome,
                  answer, cited or [], invalid, tokens, _now())

    severity = escalation.assess_severity(question)
    if severity == "acute_risk":
        log(severity, "escalated")
        return escalation.escalate_acute(member, "rag_question", question)

    def leader_question(offer_trigger):
        """
        Distress (not acute): the member still gets an answer, but it's
        followed by Stage 11's consent question instead of the casual
        offer -- settled with Keziah after testing showed honest pastoral
        questions ("I keep falling into the same sin...") are often rated
        distress, which would otherwise mean they never get an answer.
        """
        if severity == "distress":
            return escalation.start_consent_flow(whatsapp_id, "rag_question", question)
        return escalation.start_leader_offer(whatsapp_id, offer_trigger, question)

    try:
        chunks = _expand_doctrinal_basis(nearest_chunks(embed_query(_search_text(question)), TOP_K))
        retrieved = [{"citation": c["citation"],
                      "similarity": None if c["similarity"] is None else round(float(c["similarity"]), 4)}
                     for c in chunks]
        verdict, tokens = _generate(question, chunks, pastoral)
    except Exception as e:
        try:
            print(f"RAG answer failed: {e}")
        except UnicodeEncodeError:
            print("RAG answer failed (error message omitted -- contained non-ASCII characters)")
        log(severity, "error")
        if severity == "distress":
            return f"{ERROR_TEXT}\n\n{escalation.start_consent_flow(whatsapp_id, 'rag_question', question)}"
        return ERROR_TEXT

    numbers = verdict.get("sources") or []
    valid = sorted({n for n in numbers if isinstance(n, int) and 1 <= n <= len(chunks)})
    invalid = len([n for n in numbers if not (isinstance(n, int) and 1 <= n <= len(chunks))])
    cited_chunks = [chunks[n - 1] for n in valid]
    cited = [c["citation"] for c in cited_chunks]

    if verdict.get("secondary_issue"):
        log(severity, "secondary_issue", retrieved, None, cited, invalid, tokens)
        return f"{SECONDARY_ISSUE_TEXT}\n\n{leader_question('rag_secondary_issue')}"

    answer = _strip_inline_citations(verdict.get("answer") or "")
    if not verdict.get("covered") or not cited_chunks or not answer:
        log(severity, "not_covered", retrieved, answer or None, cited, invalid, tokens)
        return f"{NOT_COVERED_TEXT}\n\n{leader_question('rag_not_covered')}"

    log(severity, "answered", retrieved, answer, cited, invalid, tokens)
    reply = f"{answer}\n\n{_source_line(cited_chunks)}"
    if pastoral or severity == "distress":
        reply += "\n\n" + leader_question("pastoral_question")
    return reply
