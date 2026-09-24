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
import json
from groq import Groq

from app.models.member import (
    get_group_members,
    get_leader_of_group,
    get_all_groups_with_leaders,
)

GROQ_MODEL = "openai/gpt-oss-20b"

_SYSTEM_PROMPT = (
    "You answer a DeKUTCU Bible Study member's question about groups, group "
    "leaders, or group membership, using ONLY the tools provided -- never guess "
    "or make up group names, leader names, or member names. If a tool returns "
    "nothing useful for the question, say so plainly rather than inventing an "
    "answer. Keep replies short and conversational, suitable for WhatsApp. "
    "WhatsApp does NOT render markdown tables, headers, or links -- for lists, "
    "use plain lines (one item per line, a dash or number prefix is fine) "
    "instead of a table. *Text between single asterisks* IS supported for bold."
)

_MAX_TOOL_ROUNDS = 3


def _tool_get_my_group(member):
    if not member["group_label"]:
        return {"placed": False, "message": "You haven't been placed into a group yet."}
    roster = get_group_members(member["group_label"])
    leader = get_leader_of_group(member["group_label"])
    return {
        "placed": True,
        "group": member["group_label"],
        "leader": leader["name"] if leader else None,
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


def _tool_get_group_roster(group_label):
    roster = get_group_members(group_label)
    if not roster:
        return {"found": False, "message": f"No group called '{group_label}' was found."}
    leader = get_leader_of_group(group_label)
    return {
        "found": True,
        "group": group_label,
        "leader": leader["name"] if leader else None,
        "members": [m["name"] for m in roster],
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
            "description": "Get the asking member's own group, its leader, and who else is in it.",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    dispatch = {"get_my_group": lambda **kw: _tool_get_my_group(member)}

    if member["leads_group_label"]:
        tools.append({
            "type": "function",
            "function": {
                "name": "get_my_led_group",
                "description": "Get the roster of the specific group the asking member leads.",
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
                "description": "Get the full member roster and leader for one specific group, by its exact label (e.g. 'Bomas #2').",
                "parameters": {
                    "type": "object",
                    "properties": {"group_label": {"type": "string"}},
                    "required": ["group_label"],
                },
            },
        })
        dispatch["get_group_roster"] = lambda **kw: _tool_get_group_roster(kw["group_label"])

    return tools, dispatch


def answer_group_question(member, message_text):
    """
    Answers a read-only question about groups/leaders/membership,
    using tool-calling scoped to what `member` is allowed to see.
    Falls back to a plain apology on any error -- never guesses.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return "Sorry, I can't look that up right now."

    tools, dispatch = _build_tools_and_dispatch(member)
    client = Groq(api_key=api_key)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": message_text},
    ]

    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=GROQ_MODEL, messages=messages, tools=tools, tool_choice="auto", temperature=0,
            )
            reply_message = response.choices[0].message

            if not reply_message.tool_calls:
                return reply_message.content or "Sorry, I couldn't find an answer to that."

            messages.append(reply_message)
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

        return "Sorry, I couldn't find an answer to that."

    except Exception as e:
        print(f"group_query failed, falling back: {e}")
        return "Sorry, I couldn't look that up right now -- please try again."
