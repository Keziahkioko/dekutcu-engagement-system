"""
app/services/group_query.py

Answers open-ended READ-ONLY questions about groups, leaders, and
group membership using LLM tool-calling, instead of a hand-coded
intent per question shape. Actions that change data (nominate a
leader, allocate, reassign) deliberately stay on the fixed-intent +
confirmation path in intent_router.py -- that distinction is the
whole point: getting a tool choice slightly wrong for a QUESTION just
means an unhelpful answer, but getting an ACTION slightly wrong means
something actually changes for someone without their confirmation.
Fixed intents stay for actions; this is for questions, which are open
-ended enough that enumerating every phrasing doesn't scale.

Tools are scoped to what the ASKING member is actually allowed to see
-- built dynamically per request based on THEIR OWN role (never
trusted from the LLM's request, or the caller could ask it to
misrepresent who's asking):
  - Every registered member: their own group (get_my_group).
  - Group leaders (leads_group_label set): their led group's roster
    too (get_my_led_group) -- this can be a DIFFERENT group from their
    own personal placement if they lead cross-area via the "other"
    override in leader_assignment.py.
  - Exec leaders (is_leader): org-wide tools -- list_groups (all
    areas or one specific area) and get_group_roster (any specific
    group's members by label).

Groq's model (same one used for intent classification) supports
OpenAI-style tool calling -- verified directly before building this.
"""

import os
import re
import json

from app.services import org_contacts
from app.services import exec_roles
from app.models.member import (
    get_group_members,
    get_leader_of_group,
    get_all_groups_with_leaders,
    find_groups,
)
from app.models.conversation_history import get_recent_conversation
from app.services.registration import AREAS
from app.services.llm_client import create_chat_completion

_SYSTEM_PROMPT = (
    "You answer a DeKUTCU Bible Study member's question about groups, group "
    "leaders, group membership, or who holds an exec office (use "
    "get_exec_committee for that -- a null holder means the office isn't "
    "recorded yet; say so, never guess a name), using ONLY the tools provided -- never guess "
    "or make up group names, leader names, or member names. Keep replies short "
    "and conversational, suitable for WhatsApp. "
    "WhatsApp does NOT render markdown tables, headers, or links -- for lists, "
    "use plain lines (one item per line, a dash or number prefix is fine) "
    "instead of a table. *Text between single asterisks* IS supported for bold.\n\n"
    "The only valid residence areas are: " + ", ".join(AREAS) + ". If the "
    "member names an area loosely or misspells it (e.g. 'internal' or "
    "'nyaribo'), match it to the closest one of these before calling a tool "
    "-- don't pass their literal wording through unchanged.\n\n"
    "If asked whether the asking member IS a leader (or which group they "
    "lead): that's a completely different question from who leads the group "
    "they BELONG to. If a get_my_led_group tool is available to you at all, "
    "the answer is yes -- call it. If it is NOT available to you, the answer "
    "is no, they don't lead anything -- say so directly, don't guess based on "
    "get_my_group's leader field, which is about someone else's group entirely.\n\n"
    "MULTIPLE-GROUP AREAS -- follow this carefully, it's the most common "
    "source of mistakes. Several areas have more than one group. When a "
    "request is about an AREA rather than one specific numbered group:\n"
    "- Want a SUMMARY (sizes, leaders, 'how many', 'tell me about the groups "
    "in X') -> use list_groups for that area.\n"
    "- Specifically want MEMBER NAMES across the whole area ('names of "
    "everyone', 'all members of all groups in X') -> use get_area_rosters, "
    "NOT several separate get_group_roster calls.\n"
    "- get_group_roster comes back ambiguous (matches more than one group) "
    "and the wording didn't make clear which ONE was wanted -> ask which "
    "specific group. But if the wording already implies the whole area "
    "('the groups', 'all of them', a plural), don't ask -- go straight to "
    "list_groups or get_area_rosters per the two rules above.\n"
    "- You already asked 'which group?' and they reply 'all of them' / 'all' "
    "/ 'every group' -> that means the whole area, not one group. Use "
    "list_groups or get_area_rosters (whichever fits what they originally "
    "asked for -- summary vs. names) for the area you were just discussing.\n\n"
    "Never just say you couldn't find an answer without first trying a "
    "different tool, or asking one direct clarifying question -- only give up "
    "if the area or group genuinely doesn't exist at all.\n\n"
    "YOU ONLY ANSWER QUESTIONS -- you cannot do anything else. Never offer or "
    "promise to contact anyone, pass on a message, reach an admin, draft a "
    "message, or take any other action. Never state any contact detail (email "
    "address, phone number, website, office, group chat): your tools don't "
    "provide any, so any you wrote would be invented. There is no 'group "
    "admin'. If the member wants to reach a leader or someone in charge, tell "
    "them to say \"I'd like to talk to a leader\" and the bot will let one know; "
    "if they want DeKUTCU's official contacts, tell them to ask \"How do I "
    "contact the CU?\"."
)

# Found in the first live test: asked to help reach a "group admin", the model
# invented an office, an email address and a +1-800 phone number. The tools
# never return contact details, so ANY email, phone number or link in a reply
# was made up -- except DeKUTCU's configured official contacts. This is the
# guarantee; the prompt rule above is the first line of defence.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+\s?\d[\d\s\-‐-―()]*|\d[\d\s\-‐-―()]{6,}\d")
_LINK = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

_INVENTED_CONTACT_REPLY = (
    "I can only answer questions about groups and leaders -- I don't have anyone's "
    "contact details here. To reach a leader, just say \"I'd like to talk to a "
    "leader\" and I'll let one know, or ask \"How do I contact the CU?\" for "
    "DeKUTCU's official contacts."
)


def _has_invented_contact(reply):
    candidates = _EMAIL.findall(reply) + _PHONE.findall(reply) + _LINK.findall(reply)
    # rstrip: a sentence-ending "." or "," gets captured with an email or
    # number -- found in testing, it made the official email itself fail.
    return any(not org_contacts.is_official(c.strip().rstrip(".,;:!?)")) for c in candidates)


def _whatsapp_format(reply):
    """WhatsApp bolds *single* asterisks; Markdown's **double** ones show up literally."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", reply)


def finalise_reply(reply):
    """Public -- also used by the leadership reports (reporting.py): the same invented-contact guard and WhatsApp formatting."""
    if _has_invented_contact(reply):
        return _INVENTED_CONTACT_REPLY
    return _whatsapp_format(reply)

_FALLBACK_MESSAGE = (
    "I'm not sure how to answer that -- could you rephrase, or ask about one "
    "specific group or area?"
)

_MAX_TOOL_ROUNDS = 4


def _tool_get_my_group(member):
    if not member["group_label"]:
        return {"placed": False, "message": "You haven't been placed into a group yet."}
    roster = get_group_members(member["group_label"])
    leader = get_leader_of_group(member["group_label"])
    return {
        "placed": True,
        "group": member["group_label"],
        # Deliberately NOT called "leader" -- that reads as "is the
        # asking member A leader", which this has nothing to do with.
        # This is who leads the group THEY BELONG TO as a participant.
        "leader_of_this_group": leader["name"] if leader else None,
        "members": [m["name"] for m in roster],
    }


def _tool_get_my_led_group(member):
    if not member["leads_group_label"]:
        return {"matched": False, "message": "You haven't been matched to lead a specific group yet."}
    roster = get_group_members(member["leads_group_label"])
    return {
        "matched": True,
        "group": member["leads_group_label"],
        "members": [m["name"] for m in roster],
    }


def _tool_list_groups(area=None):
    groups = get_all_groups_with_leaders(area)
    return {
        "groups": [
            {"group": label, "area": group_area, "size": count, "leader": leader}
            for label, group_area, count, leader in groups
        ]
    }


def _tool_get_group_roster(identifier):
    """
    `identifier` can be an exact group label ("Bomas #2") OR just an
    area name ("Nyaribo", "the Nyaribo group") -- find_groups()
    resolves either. If the area has more than one group, this can't
    guess which one was meant, so it returns the list of options
    instead of a roster; the model should ask which one, or use
    list_groups to show them.
    """
    matches = find_groups(identifier)

    if not matches:
        return {"found": False, "message": f"No group or area matching '{identifier}' was found."}

    if len(matches) > 1:
        return {
            "found": False,
            "ambiguous": True,
            "matching_groups": matches,
            "message": (
                f"'{identifier}' matches more than one group: {', '.join(matches)}. "
                "Either ask the member which specific one they meant, or -- if the "
                "original request implied the whole area rather than one group -- "
                "use list_groups or get_area_rosters for this area instead."
            ),
        }

    group_label = matches[0]
    roster = get_group_members(group_label)
    leader = get_leader_of_group(group_label)
    return {
        "found": True,
        "group": group_label,
        "leader": leader["name"] if leader else None,
        "members": [m["name"] for m in roster],
    }


def _tool_get_area_rosters(area):
    """
    Every group in `area`, each with its FULL member roster -- for
    "names of everyone in every group in Bomas" style requests, so the
    model has one reliable tool instead of having to improvise
    chaining several get_group_roster calls itself.
    """
    groups = get_all_groups_with_leaders(area)
    if not groups:
        return {"found": False, "message": f"No groups found for '{area}'."}

    return {
        "found": True,
        "area": area,
        "groups": [
            {
                "group": label,
                "leader": leader,
                "members": [m["name"] for m in get_group_members(label)],
            }
            for label, group_area, count, leader in groups
        ],
    }


def _build_tools_and_dispatch(member):
    """
    Returns (tool_schemas, dispatch) -- dispatch maps a tool name to a
    zero/kwarg callable, scoped to what THIS member can access. Tools
    the member isn't allowed to use aren't just refused, they're never
    even offered to the model.
    """
    tools = [{
        "type": "function",
        "function": {
            "name": "get_my_group",
            "description": (
                "Get the group the asking member BELONGS TO as a participant, who "
                "leads that group, and who else is in it. Does NOT tell you whether "
                "the asking member themselves is a leader of anything -- for that, "
                "use get_my_led_group if it's available."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }, {
        "type": "function",
        "function": {
            "name": "get_exec_committee",
            "description": (
                "Get who holds each Executive Committee office (chairperson, vice "
                "chairpersons, secretaries, finance secretary, ministry directors). "
                "Names only. Offices with holder null are vacant or not yet recorded."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    dispatch = {
        "get_my_group": lambda **kw: _tool_get_my_group(member),
        # Exec names are public within the CU, so every member gets this --
        # names only; personal numbers are never shared with members.
        "get_exec_committee": lambda **kw: {
            "offices": [{"office": office, "holder": name} for office, name in exec_roles.committee_summary()]
        },
    }

    if member["leads_group_label"]:
        tools.append({
            "type": "function",
            "function": {
                "name": "get_my_led_group",
                "description": (
                    "Get the specific group the asking member THEMSELVES leads, and "
                    "its roster. This tool being available at all means the answer to "
                    "'am I a leader?' is yes -- call it to find out which group."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        })
        dispatch["get_my_led_group"] = lambda **kw: _tool_get_my_led_group(member)

    if member["is_leader"]:
        tools.append({
            "type": "function",
            "function": {
                "name": "list_groups",
                "description": "List every group with its area, size, and leader. Optionally filtered to one area.",
                "parameters": {
                    "type": "object",
                    "properties": {"area": {"type": "string", "description": "Optional area name to filter by"}},
                },
            },
        })
        dispatch["list_groups"] = lambda **kw: _tool_list_groups(kw.get("area"))

        tools.append({
            "type": "function",
            "function": {
                "name": "get_group_roster",
                "description": (
                    "Get the full member roster and leader for one specific group. "
                    "Accepts either an exact group label ('Bomas #2') or just an area "
                    "name ('Nyaribo') if that area only has one group -- no need to "
                    "know the exact numbering."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"identifier": {"type": "string"}},
                    "required": ["identifier"],
                },
            },
        })
        dispatch["get_group_roster"] = lambda **kw: _tool_get_group_roster(kw["identifier"])

        tools.append({
            "type": "function",
            "function": {
                "name": "get_area_rosters",
                "description": (
                    "Get EVERY group in an area, each with its full member roster in "
                    "one call. Use specifically when member NAMES are wanted across "
                    "multiple/all groups in an area (e.g. 'names of everyone in every "
                    "group in Bomas', or 'all of them' in reply to a 'which group?' "
                    "question that was about members). For just sizes/leaders with no "
                    "names, use list_groups instead -- it's shorter and usually enough."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"area": {"type": "string"}},
                    "required": ["area"],
                },
            },
        })
        dispatch["get_area_rosters"] = lambda **kw: _tool_get_area_rosters(kw["area"])

    return tools, dispatch


def answer_group_question(member, message_text):
    """
    Answers a read-only question about groups/leaders/membership,
    using tool-calling scoped to what `member` is allowed to see.
    Falls back to a plain apology on any error -- never guesses.

    Prepends recent conversation history (see conversation_history.py)
    ahead of the current question, so a short follow-up ("Internal
    means Internal Hostels") can be understood in context instead of
    read in isolation.
    """
    tools, dispatch = _build_tools_and_dispatch(member)

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for entry in get_recent_conversation(member["whatsapp_id"]):
        messages.append({"role": entry["role"], "content": entry["message_text"]})
    messages.append({"role": "user", "content": message_text})

    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            response = create_chat_completion(
                messages=messages, tools=tools, tool_choice="auto", temperature=0,
            )
            reply_message = response.choices[0].message

            if not reply_message.tool_calls:
                return finalise_reply(reply_message.content or _FALLBACK_MESSAGE)

            # Stored as a minimal plain dict, not the SDK's own message
            # object and NOT a full model_dump() -- if a later round in
            # this same loop falls back to a DIFFERENT provider (see
            # llm_client.py), that provider needs to serialize the whole
            # message list, including this one. A full model_dump() was
            # tried first and broke the normal Groq-only case: it
            # includes extra fields (e.g. "annotations") that Groq's own
            # API doesn't recognize and rejects when they're fed back in
            # on the NEXT round. Only role/content/tool_calls are ever
            # actually needed here.
            assistant_message = {"role": "assistant", "content": reply_message.content}
            if reply_message.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in reply_message.tool_calls
                ]
            messages.append(assistant_message)
            for call in reply_message.tool_calls:
                func = dispatch.get(call.function.name)
                if func is None:
                    result = {"error": f"Unknown tool {call.function.name}"}
                else:
                    args = json.loads(call.function.arguments or "{}")
                    result = func(**args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result),
                })

        return _FALLBACK_MESSAGE

    except Exception as e:
        print(f"group_query failed, falling back: {e}")
        return "Sorry, I couldn't look that up right now -- please try again."
