"""
app/services/help_guide.py

The self-taught guide to the bot (Keziah, 2026-10-16; wording approved by
her the same day). "help" gives a numbered menu of what THIS person can do
-- the member items, then group leader, Guides Coordinator and exec leader
items for whoever holds those roles -- and each item has a short guide:
  - "explain 3" / "tell me more about 3" / "option 3" -> guide 3, any time;
  - a bare "3" -> guide 3, but only when the bot's last message was the menu
    (elsewhere a number keeps its usual meaning);
  - "how do I RSVP?" (intent app_help) -> the small model picks the matching
    guide from the person's own menu.

Role safety: a guide is only ever shown to someone who can use it. Leader
items are never explained to members -- the same rule as the router's
"don't confirm a leader feature exists to a non-leader" (intent_router.py).

ALL guide text lives here: when a feature changes, change its guide in the
same commit. tests/test_help_guide.py checks every guide points at a real
intent, so a renamed or removed feature can't leave a guide behind.
"""

import json
import re

MENU_HEADER = "Here's what I can help with 🙂 Reply with a number to learn more:"
_MENU_FOOTER = "Or just type what you need in your own words. Say CANCEL anytime to stop what you're doing."
_SECTION_TITLES = {
    "group_leader": "As a group leader:",
    "coordinator": "As the Guides Coordinator:",
    "leader": "As an exec leader:",
}
_ROLE_ORDER = ["member", "group_leader", "coordinator", "leader"]


def bible_study_self_report():
    from app.services.fellowship_checkin import bible_study_self_report as setting   # lazy: no import cycle
    return setting()


def _checkins_body():
    days = "Monday, Wednesday, Thursday, Friday"
    when = f"After fellowships ({days}) and Bible Study on Tuesday" if bible_study_self_report() \
        else f"After fellowships ({days})"
    return (
        f"{when}, I'll ask \"Were you at … today?\" -- near the end of the session, or at 9pm.\n"
        "• Reply *YES* if you were there -- I'll then ask how it went.\n"
        "• If you missed it, just tell me why (e.g. \"I had a CAT\") -- it helps the leaders support you, "
        "and your own words never appear in reports.\n"
        "• Text *STOP* to pause check-ins, *RESUME* to start them again."
    )


def _group_reports_body():
    body = (
        "Ask in your own words: \"How is my group's attendance?\" · \"Who has been missing?\" · "
        "\"Why are people missing?\"\n"
        "You'll see reasons as categories (e.g. \"academic\"), never members' own words. "
        "Say \"send me the reports link\" to see charts on the reports website."
    )
    if not bible_study_self_report():
        body += "\nOn Tuesday evenings I'll ask you who was absent from Bible Study."
    return body


# Each guide: role, menu title, body (text, or a function for parts that depend on a setting), and the
# intents it explains -- used by the test that keeps the guides in step with the features.
GUIDES = [
    # --- members ---------------------------------------------------------------------------------
    {"key": "faith", "role": "member", "title": "Questions about faith and DeKUTCU",
     "intents": ["general_question", "pastoral_question"],
     "body": (
         "Ask me anything about what DeKUTCU believes, how it's run (the constitution and Code of Conduct), "
         "the Bible, or a personal question about your walk with God.\n"
         "_Try:_ \"What does DeKUTCU believe about salvation?\" · \"What does the Bible say about anxiety?\" · "
         "\"John 3:16\"\n"
         "I answer from DeKUTCU's own documents, the Bible and trusted Reformed books -- and show where each "
         "answer comes from. On questions where Christians differ (like baptism), I explain each view fairly; "
         "DeKUTCU doesn't take a side. If you're struggling, I can connect you with a leader.")},
    {"key": "group", "role": "member", "title": "Your Bible Study group",
     "intents": ["group_query"],
     "body": (
         "Ask which group you're in, who leads it, or who's in it -- or who holds each exec office.\n"
         "_Try:_ \"Which group am I in?\" · \"Who is my group leader?\" · \"Who is the chairperson?\"\n"
         "New members are placed in a group by the leaders -- if you haven't been yet, you will be soon. "
         "Leaders' personal numbers are never shared; say \"talk to a leader\" and I'll connect you.")},
    {"key": "events", "role": "member", "title": "Events and RSVPs",
     "intents": ["list_events", "event_rsvp"],
     "body": (
         "Ask what's coming up, and tell me if you're coming.\n"
         "_Try:_ \"Any events this week?\" · \"Is there fellowship today?\" · \"I'll come to worship night\"\n"
         "When you RSVP I'll ask yes, no or maybe; if several events are open, I'll show a numbered list to "
         "pick from.")},
    {"key": "checkins", "role": "member", "title": "Attendance check-ins",
     "intents": ["checkin_response", "unsubscribe_followup", "resume_followup"],
     "body": _checkins_body},
    {"key": "study_guide", "role": "member", "title": "Buying the study guide",
     "intents": ["purchase_study_guide"],
     "body": (
         "Say \"buy the study guide\". I'll tell you this semester's title and price, then send an M-Pesa "
         "prompt to your number (or another Safaricom number you give me) -- enter your PIN on your phone to pay.\n"
         "Once paid, collect your copy from your group leader; when they hand it to you, I'll ask you to "
         "confirm you received it.\n"
         "_Try:_ \"I want the study guide\" · \"Have I paid for my guide?\"")},
    {"key": "feedback", "role": "member", "title": "Feedback and questions for the leaders",
     "intents": ["feedback_response"],
     "body": (
         "After a session you attended, I'll ask how it went -- reply with anything: praise, a complaint, a "
         "suggestion, a question. Feedback is *anonymous*: leaders see what was said, never who said it.\n"
         "If you ask a question, I'll try to answer it from DeKUTCU's materials; if that isn't enough, reply "
         "*ASK* and I'll pass it to the leaders anonymously, then send you their answer.")},
    {"key": "leader_contact", "role": "member", "title": "Talking to a leader",
     "intents": ["request_human", "contact_info", "needs_support"],
     "body": (
         "Say \"I'd like to talk to a leader\" and I'll let your leaders know -- one of them will reach out to "
         "you, and I'll tell you who.\n"
         "For DeKUTCU's official contacts (office, email), ask \"How do I contact DeKUTCU?\".\n"
         "If you're in danger or thinking of harming yourself, please call *1199* (Kenya Red Cross, free) or "
         "*999 / 112* right away.")},
    {"key": "details", "role": "member", "title": "Changing your details or number",
     "intents": ["update_details", "change_number"],
     "body": (
         "• *Area:* say \"I've moved to a new area\" and pick your new one. If you're already in a group, the "
         "leaders decide whether to move you.\n"
         "• *New WhatsApp number:* tell me *from your old number first* -- \"I'm changing my number\" -- and "
         "your account moves as soon as your new number messages me. Already lost the old one? Message me "
         "from the new number with your registration number, and a leader will confirm it's you.")},
    {"key": "data", "role": "member", "title": "Your data and stopping messages",
     "intents": ["unsubscribe_followup", "withdraw_data_consent"],
     "body": (
         "• *STOP* pauses attendance check-ins only -- you'll still get announcements and can still use "
         "everything. *RESUME* starts them again.\n"
         "• To leave completely, say \"withdraw my consent\". I'll explain exactly what that removes and ask "
         "you to confirm. Your own words are deleted and the rest is made anonymous -- nothing links back to you.")},

    # --- group leaders ---------------------------------------------------------------------------
    {"key": "gl_announce", "role": "group_leader", "title": "Announcing to your group",
     "intents": ["send_announcement"],
     "body": (
         "Say \"I want to announce to my group\", type the message, and you'll see a preview -- reply YES to "
         "send it to everyone in your group. I'll tell you how many it reached. CANCEL stops it.")},
    {"key": "gl_reports", "role": "group_leader", "title": "Your group's attendance and reports",
     "intents": ["leadership_query", "reports_website"],
     "body": _group_reports_body},
    {"key": "gl_guides", "role": "group_leader", "title": "Study guides for your group",
     "intents": ["hand_over_guides"],
     "body": (
         "• When the Coordinator gives you copies, I'll ask you to confirm: reply *RECEIVED 10* (or however "
         "many you actually got). Only confirmed copies count as your stock.\n"
         "• To hand guides out, say \"hand over guides\" -- you'll get a numbered list of members who've paid; "
         "reply with the numbers (\"2, 5\"). Each member is then asked to confirm they received it.")},
    {"key": "gl_care", "role": "group_leader", "title": "When a member needs support",
     "intents": ["needs_support", "request_human"],
     "body": (
         "If one of your members asks for help or seems to be struggling, I'll send you their name, number and "
         "what they need. Reach out, then reply *CLAIM* with the case number so the leaders know it's handled.\n"
         "If nobody claims it, it goes to the exec leaders (urgent cases within 30 minutes and 2 hours; others "
         "within 2 and 6 hours).")},

    # --- Guides Coordinator ----------------------------------------------------------------------
    {"key": "co_batches", "role": "coordinator", "title": "Giving guides to group leaders",
     "intents": ["give_guide_batch"],
     "body": (
         "Say \"give guides to a leader\", pick the leader from the numbered list, say how many copies, and "
         "reply YES. The leader confirms with RECEIVED -- if they report a different number you're told at "
         "once; if they don't confirm, they're reminded after a day and you're told after 3 days.")},
    {"key": "co_ungrouped", "role": "coordinator", "title": "Members with no group leader",
     "intents": ["hand_over_guides"],
     "body": (
         "Members who aren't in a group collect from you. Say \"hand over guides\" to see who has paid and is "
         "waiting, then reply with the numbers of those you've given a copy to.")},
    {"key": "co_stock", "role": "coordinator", "title": "Stock, and starting a new guide",
     "intents": ["guide_stock", "start_study_guide"],
     "body": (
         "Say \"guide stock\" to see each leader's copies -- received, handed out, in hand -- and who's still "
         "waiting. Say \"start a new study guide\" at the start of a semester: you'll give the title and price "
         "(normally KES 70), and it goes on sale.")},

    # --- exec leaders ----------------------------------------------------------------------------
    {"key": "ex_announce", "role": "leader", "title": "Announcements and events",
     "intents": ["send_announcement", "create_event"],
     "body": (
         "Say \"I want to announce…\". Choose *1* for something on a date (an event members can RSVP to) or *2* "
         "for just a message, then who gets it -- everyone, or one area. You'll see a preview; YES sends it. "
         "Members who paused check-ins still get announcements.")},
    {"key": "ex_checkin", "role": "leader", "title": "Sending today's check-in",
     "intents": ["send_checkin"],
     "body": (
         "Near the end of a fellowship, say \"send the check-in\" -- after you confirm, everyone gets "
         "\"Were you at … today?\" straight away. Otherwise it goes out automatically at 9pm.")},
    {"key": "ex_reports", "role": "leader", "title": "Reports",
     "intents": ["leadership_query", "reports_website"],
     "body": (
         "Ask in your own words: \"Who's been missing fellowships?\" · \"Any unclaimed cases?\" · "
         "\"What feedback did we get?\" · \"How many members do we have?\"\n"
         "A summary also comes every Sunday at 8am; \"send me the reports link\" gives the reports website. "
         "Members' own words about their struggles never appear in reports, and feedback never shows who said it.")},
    {"key": "ex_care", "role": "leader", "title": "Care cases",
     "intents": ["needs_support", "request_human"],
     "body": (
         "When a member needs support, their group leader is told -- or all exec leaders, if they have no "
         "group. Reply *CLAIM* with the case number to take it. If nobody claims it, the chairpersons are told "
         "(urgent cases within 2 hours, others within 6).")},
    {"key": "ex_questions", "role": "leader", "title": "Members' questions",
     "intents": ["feedback_response"],
     "body": (
         "Questions DeKUTCU's materials don't answer come to you *anonymously*. Reply *ANSWER* with the number "
         "and your answer (e.g. \"ANSWER 12 Yes, it's open to everyone\"). The first answer goes to the member; "
         "the other leaders are told it's answered.")},
    {"key": "ex_numbers", "role": "leader", "title": "Number changes",
     "intents": ["change_number"],
     "body": (
         "When a member can't confirm a move to a new number from their old phone, you'll be asked to check "
         "(in person or by a call) -- reply *APPROVE* or *DENY* with the number. Leader accounts always need an "
         "exec leader's approval.")},
    {"key": "ex_groups", "role": "leader", "title": "Groups",
     "intents": ["allocate_groups", "reshuffle_groups", "resolve_reassignments"],
     "body": (
         "• \"Allocate the new members\" places ungrouped members into Bible Study groups (8–12, mixed by gender "
         "and year).\n"
         "• \"Reshuffle the groups\" rebuilds every group from scratch (you'll be asked to confirm).\n"
         "• When a placed member moves area, you're asked to approve the suggested new group: "
         "\"resolve reassignments\".")},
    {"key": "ex_group_leaders", "role": "leader", "title": "Group leaders",
     "intents": ["nominate_group_leader", "view_group_leaders", "remove_group_leader", "resolve_pending_leader"],
     "body": (
         "\"Nominate a group leader\" (pick the area, then the person) · \"Show the group leaders\" · "
         "\"Remove a group leader\" · \"Confirm a pending leader\" (when they agreed in person).")},
    {"key": "ex_offices", "role": "leader", "title": "Exec offices",
     "intents": ["set_exec_roles"],
     "body": (
         "After the AGM, say \"record the exec roles\" -- pick each office, then its holder. Holding an office "
         "gives leader access; losing it takes that away. Members can then ask \"Who is the chairperson?\".")},
    {"key": "ex_guides", "role": "leader", "title": "Study guides",
     "intents": ["start_study_guide", "guide_stock", "appoint_guides_coordinator"],
     "body": (
         "\"Start a new study guide\" · \"Guide stock\" · \"Appoint the guides coordinator\" (Discipleship "
         "Director or exec).")},
]
_BY_KEY = {g["key"]: g for g in GUIDES}


def _roles(member):
    from app.services.guide_coordinator import is_coordinator
    roles = {"member"}
    if member.get("leads_group_label"):
        roles.add("group_leader")
    if is_coordinator(member):
        roles.add("coordinator")
    if member.get("is_leader"):
        roles.add("leader")
    return roles


def menu_for(member):
    """This person's guides, in menu order -- position + 1 is the number they reply with."""
    roles = _roles(member)
    return [g for role in _ROLE_ORDER if role in roles for g in GUIDES if g["role"] == role]


def menu_text(member):
    lines, section = [MENU_HEADER, ""], "member"
    for number, guide in enumerate(menu_for(member), start=1):
        if guide["role"] != section:
            section = guide["role"]
            lines += ["", _SECTION_TITLES[section]]
        lines.append(f"{number}. {guide['title']}")
    return "\n".join(lines + ["", _MENU_FOOTER])


def _body(guide):
    return guide["body"]() if callable(guide["body"]) else guide["body"]


def guide_text(guide):
    return f"*{guide['title']}*\n\n{_body(guide)}\n\n(Say *help* to see the menu again.)"


def explain_number(member, number):
    menu = menu_for(member)
    if 1 <= number <= len(menu):
        return guide_text(menu[number - 1])
    return f"There's no option {number} -- pick a number from 1 to {len(menu)}.\n\n" + menu_text(member)


# "explain 3", "tell me more about 3", "option 3", "number 3", "more on 3", "3 please" ... -- any time.
_EXPLAIN = re.compile(
    r"^(?:explain|tell me (?:more )?about|more (?:on|about)|option|number|no\.?|show me|guide)\s*(?:option|number)?\s*#?(\d{1,2})"
    r"(?:\s*please)?[.!?]*$")
_BARE_NUMBER = re.compile(r"^#?(\d{1,2})[.!]?$")


def menu_choice(text, after_menu):
    """
    The guide number they asked for, or None. A bare number only counts when `after_menu` is True (the bot's
    last message was the menu) -- otherwise "3" may mean something else entirely.
    """
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
    match = _EXPLAIN.match(cleaned)
    if match:
        return int(match.group(1))
    if after_menu:
        match = _BARE_NUMBER.match(cleaned)
        if match:
            return int(match.group(1))
    return None


def last_reply_was_menu(whatsapp_id):
    from app.models.conversation_history import get_recent_conversation
    recent = get_recent_conversation(whatsapp_id, limit=1)
    return bool(recent) and recent[-1]["role"] == "assistant" and recent[-1]["message_text"].startswith(MENU_HEADER)


# --- "how do I ...?" (intent app_help) ------------------------------------------------------------

_MATCH_PROMPT = (
    "A member of a university Christian Union is asking how to use its WhatsApp bot. Below is the numbered list "
    "of guides available to THEM. Pick the ONE guide that answers their question. If none clearly does, answer 0.\n"
    "Reply in JSON: {\"guide\": <number>}"
)


def answer_how_to(member, question):
    """The app_help intent: the matching guide from this person's own menu -- or the menu if none fits."""
    from app.services.llm_client import create_chat_completion
    menu = menu_for(member)
    listing = "\n".join(f"{n}. {g['title']}" for n, g in enumerate(menu, start=1))
    try:
        response = create_chat_completion(
            messages=[{"role": "system", "content": _MATCH_PROMPT},
                      {"role": "user", "content": f"Guides:\n{listing}\n\nQuestion: {question}"}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        number = int(json.loads(response.choices[0].message.content).get("guide") or 0)
    except Exception as e:
        print(f"Help-guide match failed: {type(e).__name__}")
        number = 0
    if 1 <= number <= len(menu):
        return guide_text(menu[number - 1])
    return menu_text(member)
