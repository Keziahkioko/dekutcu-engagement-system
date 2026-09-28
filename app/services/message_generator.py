"""
app/services/message_generator.py

Stage 9 (fuller scope): generates the actual message text for a
chosen bandit arm, live, via the LLM -- given the member's OWN stated
reason for missing an activity (their raw words, not just the
category bucket), so the message can respond to what they actually
said instead of reading as generic per-category boilerplate.

Deliberately a separate module from bandit.py: the bandit's own
arm-selection logic stays pure local math with no API calls (see
bandit.py's docstring / PROJECT_LOG.md's "confirmed non-issue" note),
and this is the one piece that actually talks to an LLM.
select_arm_for_absence returns just the chosen arm name now -- this
module is what turns that into text.

Kept viable against the shared Groq daily token budget specifically
because this only fires once per absence reply -- a naturally
rate-limited event (weekly, per person, only for people who actually
missed something) -- unlike classify_intent, which runs on every
single incoming message and is what actually exhausted the daily
quota before (see PROJECT_LOG.md).

Falls back to a fixed template per arm (what this stage started from)
on ANY generation failure -- a member should never be left with no
message because a live call failed.
"""

from app.services.llm_client import create_chat_completion

ARM_DESCRIPTIONS = {
    "reminder": "a gentle nudge that we'd love to see them next time.",
    "empathetic_checkin": (
        "acknowledge missing them personally and warmly, checking in on "
        "how they're doing, not just their attendance."
    ),
    "barrier_support": (
        "offer to help work around whatever's making it hard to attend "
        "(transport, timing, etc.) -- practical, not vague."
    ),
    "peer_connection": (
        "offer to have someone from their group reach out personally to "
        "catch up."
    ),
}

# Same wording this stage started from -- kept as the safety net if a
# live generation call ever fails, not thrown away.
_FALLBACK_MESSAGES = {
    "reminder": (
        "Just a gentle reminder that we'd love to see you at the next one! "
        "Hope to see you there."
    ),
    "empathetic_checkin": (
        "We really missed having you around. However you're doing, we just "
        "want you to know we're thinking of you."
    ),
    "barrier_support": (
        "If there's anything making it hard to attend -- transport, timing, "
        "anything -- let us know. We'd love to help figure something out."
    ),
    "peer_connection": (
        "Would it help if someone from your group reached out to catch up "
        "with you personally? Just say the word."
    ),
}

# The small, fixed set of activity_type keys this project actually
# uses (see attendance.py / fellowship_checkin.py) mapped to a display
# name -- absences only store the raw key, not a pretty name, so this
# is the one place that resolves it back for message text. Falls back
# to a title-cased version of the key itself for anything unexpected,
# rather than erroring, since this is just cosmetic phrasing.
ACTIVITY_DISPLAY_NAMES = {
    "bible_study": "Bible Study",
    "monday_fellowship": "Monday Fellowship",
    "wednesday_prayers": "Wednesday Prayers",
    "thursday_fellowship": "Thursday Fellowship",
    "friday_fellowship": "Friday Fellowship",
    "sunday_service": "Sunday Service",
}

_SYSTEM_PROMPT_TEMPLATE = """You write a short, warm WhatsApp message to a DeKUTCU (Christian Union)
member who just missed an activity and shared their reason why. Match
this tone exactly: warm, personal, never guilt-tripping, 1-2 sentences,
no corporate/customer-service phrasing.

Scripture may be referenced when it fits naturally -- one verse or a
short paraphrase at most, never preachy or lecturing. When you do,
frame it from a Reformed theological viewpoint (e.g. God's sovereignty
and grace carrying someone through, not framed around willpower or
self-effort).

{name_line} Never write a placeholder like "[Name]" -- if no real name
is given, just don't use a name at all.

The strategy to use is: {arm}
  - {arm_description}

{reason_line}

Write ONE message only, matching the strategy above, responding
naturally to what they actually said. No preamble, no quotation marks,
just the message text itself."""


def display_name_for(activity_type):
    """Public -- also used by the feedback prompts, so every activity is named the same way everywhere."""
    return ACTIVITY_DISPLAY_NAMES.get(activity_type, activity_type.replace("_", " ").title())


def generate_arm_message(arm, activity_type, reason_text=None, member_name=None):
    """
    Live-generates the message for a chosen arm, given the member's own
    stated reason (if any -- 'skip' replies and silent-lapse absences
    have none). Falls back to a fixed per-arm template on any failure.

    member_name is optional (mirrors reason_capture.build_reason_prompt's
    own name=None pattern) -- found via real testing that without an
    explicit real name OR an explicit instruction not to invent one, the
    model fabricates a literal "[Name]" placeholder, which would go out
    verbatim to a real member. Only the first name is ever passed in,
    same convention already used elsewhere in this project.
    """
    activity_display_name = display_name_for(activity_type)

    if reason_text:
        reason_line = f'Their stated reason for missing {activity_display_name}: "{reason_text}"'
    else:
        reason_line = f"They didn't give a specific reason for missing {activity_display_name}."

    first_name = member_name.split()[0] if member_name else None
    name_line = (
        f"Their first name is {first_name} -- you may address them by it naturally."
        if first_name else
        "No name is available for this member -- don't address them by name at all."
    )

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        arm=arm, arm_description=ARM_DESCRIPTIONS[arm], reason_line=reason_line, name_line=name_line,
    )

    try:
        response = create_chat_completion(
            messages=[{"role": "system", "content": system_prompt}],
            temperature=0.7,
        )
        text = response.choices[0].message.content.strip()
        return text if text else _FALLBACK_MESSAGES[arm]
    except Exception as e:
        try:
            print(f"Message generation failed, falling back to fixed template: {e}")
        except UnicodeEncodeError:
            print("Message generation failed, falling back to fixed template (error message omitted -- contained non-ASCII characters)")
        return _FALLBACK_MESSAGES[arm]
