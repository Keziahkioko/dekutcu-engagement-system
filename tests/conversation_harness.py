"""
tests/conversation_harness.py

Simulates students talking to the bot over the REAL webhook routing
(route_incoming_message), so multi-message conversations and state
transitions are tested end to end -- not just single functions.

Safety:
  - runs ONLY against the DEMO database (demo/demo_safety.py refuses
    unless DEMO_DATABASE_URL is a different server from DATABASE_URL);
  - every WhatsApp send is captured, never sent;
  - Safaricom (STK push / status query) is faked -- no real payments;
  - every AI call is replaced by a deterministic fake, so results are
    repeatable and no free-tier quota is used. Any AI call the harness
    hasn't faked raises immediately (UnexpectedLLMCall), so a new
    un-mocked path can't silently reach Groq. Natural-language
    understanding is evaluated separately against the real model
    (tests/eval_intent_routing.py).

Test members are TEST-QA-* rows, removed by cleanup().
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from demo.demo_safety import demo_database_url

os.environ["DATABASE_URL"] = demo_database_url()
os.environ["RUN_BACKGROUND_WORKERS"] = "false"
os.environ.pop("RENDER", None)


class UnexpectedLLMCall(Exception):
    pass


# --- deterministic stand-ins for the AI --------------------------------------------------------

# Ordered (pattern, intent) rules standing in for the intent classifier. They only need to be good
# enough to START each flow the tests exercise; real-language routing is evaluated separately.
INTENT_RULES = [
    (r"\b(kill myself|end my life|suicid)", "needs_support"),
    (r"\b(struggling|depressed|can't cope|hopeless)\b", "needs_support"),
    (r"\b(talk to (a )?(leader|human|person)|speak to someone)\b", "request_human"),
    (r"\bstart (a )?new study guide\b", "start_study_guide"),
    (r"\b(buy|purchase|pay for)\b.*\bguide\b", "purchase_study_guide"),
    (r"\b(guides? coordinator|in charge of the guides|handle the guides)\b", "appoint_guides_coordinator"),
    (r"\b(give guides|record a batch|gave .* copies)\b", "give_guide_batch"),
    (r"\b(hand over guides|who needs guides)\b", "hand_over_guides"),
    (r"\bguide stock\b", "guide_stock"),
    (r"\b(changing my number|change my number|new (whatsapp )?number|new line)\b", "change_number"),
    (r"\b(announce|announcement|send a notice|tell (all|every))", "send_announcement"),
    (r"\b(rsvp)\b", "event_rsvp"),
    (r"\b(create|add|announce) (an? )?(new )?event\b", "create_event"),
    (r"\b(what|which|any|upcoming).*\bevents?\b", "list_events"),
    (r"\b(stop|pause) (my )?check-?ins\b", "unsubscribe_followup"),
    (r"\bresume (my )?check-?ins\b", "resume_followup"),
    (r"\bwithdraw\b", "withdraw_data_consent"),
    (r"\b(change|update) my area\b", "update_details"),
    (r"\b(set|record|update) (the )?exec\b", "set_exec_roles"),
    (r"\bnominate\b", "nominate_group_leader"),
    (r"\ballocate\b", "allocate_groups"),
    (r"\breshuffle\b", "reshuffle_groups"),
    (r"\bsend (the )?check-?in\b", "send_checkin"),
    (r"\b(contact|phone number|email)\b.*\b(cu|dekutcu)\b|\bcu contacts?\b", "contact_info"),
    (r"\b(reports? (link|website)|log me in)\b", "reports_website"),
    (r"\b(who('| i)s missing|attendance report|how is attendance)\b", "leadership_query"),
    (r"\b(my group|group leader|which group)\b", "group_query"),
    (r"\bi missed\b|\bwasn't at\b", "checkin_response"),
    (r"\b(what|who|how|when|where|why|is|does|explain)\b.*\?$", "general_question"),
    (r"^(hi|hello|hey|niaje|sasa|thanks?|thank you|good (morning|evening))\b", "greeting_smalltalk"),
]


def fake_classify_intent(text, whatsapp_id=None):
    lowered = (text or "").lower().strip()
    for pattern, intent in INTENT_RULES:
        if re.search(pattern, lowered):
            return intent
    return "unclear"


def fake_assess_severity(text):
    lowered = (text or "").lower()
    if re.search(r"kill myself|end my life|suicid", lowered):
        return "acute_risk"
    if re.search(r"struggling|depressed|can't cope|hopeless", lowered):
        return "distress"
    return "none"


def fake_classify_reason(text):
    lowered = (text or "").lower()
    for words, category in [(("sick", "clinic", "unwell", "flu"), "health"), (("class", "cat", "exam", "lab"), "scheduling_conflict"),
                            (("fare", "far", "rain"), "logistical_barrier"), (("forgot", "didn't feel"), "disengagement")]:
        if any(w in lowered for w in words):
            return category
    return "unclassified"


def fake_is_reason(text):
    lowered = (text or "").lower()
    return "?" not in lowered and bool(re.search(
        r"sick|unwell|clinic|class|cat\b|exam|lab|fare|far|rain|forgot|busy|because|couldn't|didn't|work|travel|nilikuwa class", lowered))


def fake_rag_answer(member, question, pastoral, severity):
    """Stands in for rag_companion._answer (search + generation) -- the real answer_question wrapper still runs,
    so the safety-first check and the "(Taking that as ...)" line are exercised. Logs the query like the real one,
    so the follow-up window (asked the companion in the last 30 minutes) works."""
    from app.models.rag import log_query
    log_query(member["reg_number"], question, "pastoral" if pastoral else "general", severity, [], "answered",
              "fake", [], 0, None, now_utc())
    return f"[RAG answer to: {question}]"


def fake_condense(messages=None, **kwargs):
    """Stands in for the follow-up rewrite: '... verse N' after a 'Book C:V' question -> 'What does Book C:N mean?'."""
    content = messages[-1]["content"]
    latest = content.rsplit("Latest message: ", 1)[1]
    refs = re.findall(r"Member: .*?\b((?:[1-3] )?[A-Z][a-z]+) (\d+):\d+", content)
    verse = re.search(r"verse (\d+)", latest.lower())
    question = f"What does {refs[-1][0]} {refs[-1][1]}:{verse.group(1)} mean?" if refs and verse else latest

    class _Msg:
        def __init__(self, text):
            self.content = text

    class _Choice:
        def __init__(self, text):
            self.message = _Msg(text)

    class _Resp:
        choices = [_Choice(json.dumps({"question": question}))]
    return _Resp()


def fake_answer_from_materials(member, question):
    return None


def fake_arm_message(arm, activity_type, reason_text=None, member_name=None):
    return f"[{arm} follow-up for {activity_type}]"


def fake_group_question(*args, **kwargs):
    return "[group answer]"


def fake_leadership_question(member, text):
    return "[leadership report]"


def _llm_guard(*args, **kwargs):
    raise UnexpectedLLMCall("an AI call reached the model during a deterministic test -- mock it in the harness")


# --- Safaricom fake ------------------------------------------------------------------------------

class FakeSafaricom:
    def __init__(self):
        self.pushes, self.results = [], {}

    def push(self, phone, amount, ref, desc):
        cid = f"ws_CO_QA_{len(self.pushes) + 1}"
        self.pushes.append((phone, amount))
        self.results[cid] = None
        return {"checkout_request_id": cid, "merchant_request_id": "m"}

    def query(self, cid):
        r = self.results.get(cid)
        return None if r is None else {"result_code": r, "result_desc": "fake"}


# --- the harness --------------------------------------------------------------------------------

_SEND_MODULES = [
    "app.services.whatsapp_client", "app.routes.webhook", "app.services.area_change", "app.services.attendance",
    "app.services.escalation", "app.services.event_manager", "app.services.exec_roles", "app.services.fellowship_checkin",
    "app.services.intent_router", "app.services.leader_assignment", "app.services.member_questions",
    "app.services.reporting", "app.services.study_guides", "app.services.withdrawal", "app.services.announcements",
    "app.services.number_change", "app.services.guide_coordinator", "app.services.guide_batches",
    "app.services.guide_handover",
]
_LLM_MODULES = [
    "app.services.escalation", "app.services.feedback", "app.services.feedback_themes", "app.services.group_query",
    "app.services.intent_router", "app.services.message_generator", "app.services.rag_companion",
    "app.services.reason_capture", "app.services.reporting",
]


class Harness:
    """Start once per test module: patches everything external, gives `say()` and outbox helpers."""

    def __init__(self):
        self.outbox = []          # every WhatsApp message the bot tried to send: (to, text)
        self.safaricom = FakeSafaricom()
        self._patches = []

    def _send(self, to_number=None, message_text=None, *args, **kwargs):
        if to_number is None and args:
            to_number = args[0]
        self.outbox.append((to_number, message_text))
        message_id = f"wamid.out.{len(self.outbox)}"

        class _Ok:
            status_code = 200

            def json(self):
                return {"messages": [{"id": message_id}]}
        return _Ok()

    def start(self):
        for m in _SEND_MODULES:
            self._patches.append(patch(f"{m}.send_whatsapp_message", self._send))
        for m in _LLM_MODULES:
            self._patches.append(patch(f"{m}.create_chat_completion", _llm_guard))
        self._patches += [
            patch("app.services.intent_router.classify_intent", fake_classify_intent),
            patch("app.services.escalation.assess_severity", fake_assess_severity),
            patch("app.services.reason_capture.classify_reason", fake_classify_reason),
            patch("app.services.reason_capture._is_reason", fake_is_reason),
            patch("app.services.reason_capture.is_reason", fake_is_reason),
            patch("app.services.rag_companion._answer", fake_rag_answer),
            patch("app.services.rag_companion.create_chat_completion", fake_condense),   # after the guard: wins
            patch("app.services.rag_companion.answer_from_materials", fake_answer_from_materials),
            patch("app.services.message_generator.generate_arm_message", fake_arm_message),
            patch("app.services.intent_router.answer_group_question", fake_group_question),
            patch("app.services.reporting.answer_leadership_question", fake_leadership_question),
            patch("app.services.mpesa.stk_push", self.safaricom.push),
            patch("app.services.mpesa.query_status", self.safaricom.query),
        ]
        for p in self._patches:
            p.start()
        from app import create_app
        self.app = create_app()
        from app.routes.webhook import route_incoming_message
        self._route = route_incoming_message
        return self

    def stop(self):
        for p in reversed(self._patches):
            p.stop()

    def say(self, whatsapp_id, text):
        reply = self._route(whatsapp_id, text)
        return reply

    def sent_to(self, whatsapp_id):
        return [t for to, t in self.outbox if to == whatsapp_id]


def q(sql, params=(), fetch=True):
    from app.database import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall() if fetch and cur.description else None
    conn.commit()
    cur.close()
    conn.close()
    return rows


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def add_member(reg, whatsapp_id, name, *, group=None, leads=None, leader=False, office=None, followup=True):
    q("""INSERT INTO members (reg_number, whatsapp_id, name, gender, year_of_study, area, data_consent, followup_consent,
                              registered_at, group_label, leads_group_label, is_leader, exec_office)
         VALUES (%s, %s, %s, 'Female', 2, 'Bomas', TRUE, %s, %s, %s, %s, %s, %s)""",
      (reg, whatsapp_id, name, followup, now_utc(), group, leads, leader, office), fetch=False)


QA_PENDING_TABLES = [
    "pending_actions", "pending_area_changes", "pending_attendance_marking", "pending_escalation_consent",
    "pending_event_creation", "pending_exec_role", "pending_feedback", "pending_fellowship_checkin",
    "pending_leader_nominations", "pending_question_ask", "pending_reason_capture",
    "pending_reassignment_resolutions", "pending_registrations", "pending_rsvps", "pending_guide_creation",
    "pending_guide_purchase", "pending_announcement", "pending_coordinator_choice", "pending_batch", "pending_handover", "last_flow_reply", "conversation_history",
]


# Parallel runs: each process sets QA_SHARD (one digit) and only ever creates/cleans its own numbers
# (2547995<shard>xxxx), reg numbers (TEST-QA-<shard>-...) and events ("QA<shard> ...").
SHARD = os.getenv("QA_SHARD", "0")[:1]
WA_PREFIX = f"2547995{SHARD}"
REG_PREFIX = f"TEST-QA-{SHARD}-"
EVENT_PREFIX = f"QA{SHARD} "


def cleanup():
    """Removes this shard's TEST-QA members and anything keyed by its WhatsApp numbers."""
    like = WA_PREFIX + "%"
    for t in QA_PENDING_TABLES:
        if _table_exists(t):      # a brand-new table only exists once the app has started
            q(f"DELETE FROM {t} WHERE whatsapp_id LIKE %s", (like,), fetch=False)
    if _table_exists("number_changes"):
        q("DELETE FROM number_changes WHERE old_whatsapp LIKE %s OR new_whatsapp LIKE %s OR reg_number LIKE %s",
          (like, like, REG_PREFIX + "%"), fetch=False)
    regs = [r["reg_number"] for r in q("SELECT reg_number FROM members WHERE reg_number LIKE %s OR whatsapp_id LIKE %s",
                                       (REG_PREFIX + "%", like))]
    if regs:
        q("DELETE FROM pending_reason_capture WHERE absence_id IN (SELECT id FROM absences WHERE reg_number = ANY(%s))", (regs,), fetch=False)
        q("DELETE FROM pending_feedback WHERE feedback_request_id IN (SELECT id FROM feedback_requests WHERE reg_number = ANY(%s))", (regs,), fetch=False)
        for t in ["absences", "fellowship_checkins", "feedback_requests", "rag_queries", "member_questions", "guide_purchases"]:
            q(f"DELETE FROM {t} WHERE reg_number = ANY(%s)", (regs,), fetch=False)
        q("DELETE FROM escalations WHERE case_id IN (SELECT id FROM escalation_cases WHERE reg_number = ANY(%s))", (regs,), fetch=False)
        q("DELETE FROM escalations WHERE reg_number = ANY(%s) OR notified_leader_reg_number = ANY(%s)", (regs, regs), fetch=False)
        q("DELETE FROM escalation_cases WHERE reg_number = ANY(%s)", (regs,), fetch=False)
        if _table_exists("announcements"):
            q("DELETE FROM announcements WHERE sent_by_reg_number = ANY(%s)", (regs,), fetch=False)
        if _table_exists("guide_coordinator"):
            q("DELETE FROM guide_coordinator WHERE reg_number = ANY(%s)", (regs,), fetch=False)
        q("DELETE FROM members WHERE reg_number = ANY(%s)", (regs,), fetch=False)
    q("DELETE FROM event_rsvps WHERE whatsapp_id LIKE %s", (like,), fetch=False)
    q("DELETE FROM event_rsvps WHERE event_id IN (SELECT id FROM events WHERE title LIKE %s)", (EVENT_PREFIX + "%",), fetch=False)
    q("DELETE FROM events WHERE title LIKE %s", (EVENT_PREFIX + "%",), fetch=False)
    q("DELETE FROM pending_messages WHERE sender_number LIKE %s", (like,), fetch=False)
    if _table_exists("processed_messages"):
        q("DELETE FROM processed_messages WHERE message_id LIKE %s", (f"wamid.QA{SHARD}%",), fetch=False)


def _table_exists(name):
    return bool(q("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (name,)))
