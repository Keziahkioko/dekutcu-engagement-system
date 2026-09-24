# DeKUTCU Engagement System — Project Log

Living record of build status and design decisions for this capstone, so
context survives across sessions (including AI sessions — see the note at
the bottom of `CLAUDE.md`). This file records **decisions and their
reasoning**, not full conversations or code detail — the code itself is
the source of truth for *how* something works; this file is for *why*,
and for *what stage things are at*.

**Maintenance rule:** whenever a real design decision is settled or a
stage is finished, add a short entry to the log at the bottom before
moving on to the next thing. Don't wait to be asked.

Status assessments below were determined by inspecting the actual repo
(file structure, code, `git log`) on 2026-09-24, not assumed from prior
conversation — cross-check against the repo again if this file is ever
suspected stale.

---

## Stage status

| # | Stage | Status |
|---|---|---|
| 0 | Environment & Access Setup | ✅ Done |
| 1 | Skeleton WhatsApp bot | ✅ Done |
| 2 | Data Layer | ✅ Done |
| 3 | Registration & Consent | ✅ Done |
| 4 | Intent Router | ✅ Done (core router + 4 real intents; several intents deliberately still stubbed pending their own later stage — see detail below) |
| 5 | Allocation Engine (Objective 2) | ✅ Core engine done, tested, deployed, verified live. Plus substantial extra infrastructure built alongside it (not in the original 15-stage plan) — see detail below |
| 6 | Event & RSVP Manager | ⬜ Not started |
| 7 | Reason Capture & Classifier | ⬜ Not started |
| 8 | Contextual Bandit Engine (Objective 3) | ⬜ Not started |
| 9 | Message Generator | ⬜ Not started |
| 10 | Reward Loop | ⬜ Not started |
| 11 | Escalation Manager | ⬜ Not started (but `request_human`/`needs_support` intents already exist as stubs anticipating it) |
| 12 | RAG Companion (Objective 4) | ⬜ Not started |
| 13 | Leadership Reporting Interface | ⬜ Not started as its own stage, but `group_query`'s tool-calling architecture (built during Stage 5, see below) is directly reusable groundwork for it — worth revisiting when Stage 13 starts rather than building reporting from scratch |
| 14 | M-Pesa Payments | ⬜ Not started (`purchase_study_guide` intent stubbed) |
| 15 | Full Evaluation & Report | ⬜ Not started |

---

## Stage detail

### Stage 0 — Environment & Access Setup
Python, VS Code, Git/GitHub, Meta WhatsApp Cloud API, Groq API, all working. Free-tier-only stack throughout (no budget) — see the tech-stack decision entries below.

### Stage 1 — Skeleton WhatsApp bot
Flask webhook live on Render (`dekutcu-engagement-system.onrender.com`), full WhatsApp round-trip confirmed for real. Fixed along the way: Render free-tier sleep (UptimeRobot ping every 5 min), a Meta WABA-not-subscribed-to-app bug, and 24h access-token expiry (permanently fixed via a System User permanent token).

### Stage 2 — Data Layer
`members` table. `reg_number` (unique, NOT NULL) is the true permanent identity — school-assigned, survives a phone number change. `whatsapp_id` is a nullable pointer, re-linked when a member messages from a new number. Migrated from a flat script to an app-factory Flask package, and from SQLite to PostgreSQL/Neon (Render's own Postgres free tier expires after 30 days with no backups — incompatible with the project timeline).

### Stage 3 — Registration & Consent
Full WhatsApp conversation: reg number (normalized) → name → gender → year of study → area (10 fixed areas) → **two separate consents** (data/tracking/placement, and proactive follow-up check-ins — opting out of follow-up does NOT affect active-membership/welfare status). Field-name correction at the review step; `cancel` = full restart anywhere. Returning members auto-relink by `reg_number` from a new phone.

### Stage 4 — Intent Router
STOP/RESUME keywords checked globally, hardcoded, before registration status and before the LLM — an opt-out must never depend on an AI classifier having a good day. Everything else routes through Groq-based classification. Genuinely implemented: `update_details` (area only, see Stage 5 detail), `unsubscribe_followup`, `resume_followup`, `withdraw_data_consent`, plus everything added during Stage 5 (see below). Deliberately still stubbed, pending their own stage: `general_question`, `event_rsvp`, `checkin_response`, `feedback_response`, `purchase_study_guide`, `request_human`, `needs_support`.

### Stage 5 — Allocation Engine (Objective 2)
Core engine (`app/services/allocation.py`): three interchangeable functions sharing the same sizing logic — `allocate_members_greedy` (fast round-robin baseline), `allocate_members_ilp` (PuLP/CBC, exact optimization), `allocate_members_topup` (only places *new* members, leaves existing placements alone — see decision log). Wired into WhatsApp as `allocate_groups`/`reshuffle_groups`, background-threaded (the ILP is too slow for a synchronous webhook reply), with a real leader confirmation flow. **Verified live end-to-end** against 200 real+synthetic members on the deployed Render app, not just locally.

While finishing this stage's real-world WhatsApp deployment, three pieces of infrastructure turned out to be genuine prerequisites and got built alongside it, even though they aren't named stages in the original 15-stage plan:
- **Group leader management** — recruiting and tracking who leads each area's Bible study group(s), distinct from `is_leader` (which gates exec/org actions). Needed before "your leader is X" could mean anything.
- **Area-change reassignment** — a member changing residence area needs their group membership handled without a leader losing track of it.
- **`group_query` tool-calling** — after the leader-management and reassignment work produced a growing pile of narrow intents (view leaders, resolve pending, etc.), read-only *questions* about groups/leaders/membership moved to LLM tool-calling instead of one hand-coded intent per question shape. See decision log for why, and why actions stayed on the fixed-intent path.

---

## Design decisions log

Newest entries at the bottom. Each entry: what was decided, and why — not the back-and-forth that got there.

**Tech stack is free-tier only, throughout.** No budget for this project. Groq chosen over Gemini as the primary classification LLM specifically because intent routing fires on *every* incoming message and needs Groq's higher free daily request volume; Gemini is kept available for later, lower-frequency, quality-sensitive work (e.g. the future RAG companion). Neon chosen over Render's own Postgres (which expires after 30 days on the free tier with no backups) and over Supabase/Koyeb, for auto-wake-on-query behavior and better solo-troubleshooting docs.

**`reg_number`, not `whatsapp_id`, is the permanent member identity.** Phone numbers change; school registration numbers don't. `whatsapp_id` is just a nullable pointer that gets re-linked.

**Consent is two separate flags, not one.** `data_consent` (storing details, placement, welfare tracking) and `followup_consent` (proactive check-ins) are independent — opting out of check-ins must never be interpreted as opting out of active membership.

**STOP/RESUME are hardcoded keywords, checked before anything else, including registration status.** An opt-out mechanism can't depend on an LLM classifier working correctly that day.

**Masters students and staff are out of scope for automated allocation.** Rare in practice; handled manually. The system currently only validates `year_of_study` 1–6 and has no separate staff/masters concept at all.

**10 fixed areas, hard-partitioned before optimization.** Exact string match at registration (closest-match prompt if unclear), no GPS/adjacency logic. The allocation engine treats areas as completely independent — every algorithm runs per-area, never across areas.

**Allocation group size is 8–12, not the original 6–10 spec.** Changed during direct design work on the algorithm itself (supersedes the number in any earlier planning notes). Group count per area is chosen to minimize the *worst* individual group's deviation from [8,12] (0 if a perfect fit exists), tie-broken toward more, more-evenly-sized groups — this specifically closes a gap where a strict "must fit exactly" rule leaves areas of 13/14/15 members with no valid group count at all (verified: without this, a real 200-member run mis-flagged 3 normal-sized areas as "too small").

**ILP minimizes the single worst gender/year deviation, gender and year checked separately — not jointly.** With only 8–12 people per group, joint (gender × year) cells are too sparse to usefully optimize, and the original balance objective was stated as two separate proportions anyway. Minimizing the *worst* case (minimax) rather than the *average* directly targets avoiding one badly-skewed group, which is what motivated building the ILP over the greedy version in the first place (a real greedy run produced a 6-male/3-female group in an area that was exactly 50/50 overall).

**ILP solve is capped at 15 seconds per area.** Finding a very good (often exactly optimal) answer is fast; what can run unboundedly long is the solver *proving* nothing better exists, by ruling out symmetric group-label duplicates it has no way to know are interchangeable. Uncapped, a ~45-person/5-group area was observed to hang for minutes at 200-member scale. Capped, quality is unaffected in testing.

**Top-up allocation never moves an already-placed member.** Allocation reruns periodically as members trickle in; a full reshuffle every time would break group continuity/relationships for no reason. Only genuinely new (unplaced) members get processed; leftover new members who don't fit any existing group form fresh groups of their own. `reshuffle_groups` (full, from-scratch regeneration) exists as a separate, more seriously-confirmed action for when a real reset is actually wanted.

**Group leaders are a different concept from `is_leader`.** `is_leader` gates exec/organizational actions (running allocation, reports). A group leader runs one specific area's Bible study and is a completely separate role — tracked via `leader_of_area` (confirmed) and `leads_group_label` (matched to a *specific* formed group, once allocation has actually formed enough groups in their area). Leaders are recruited **before** allocation runs (mirroring the real off-platform process: an exec leader asks around informally, then formalizes it on-platform), since they're naturally tied to an area, not a numbered group that doesn't exist yet.

**Every leader-facing "pick a person/group" flow uses a numbered list, never free-text name parsing.** Matching a name from free text requires disambiguation infrastructure (fuzzy matching, "which John did you mean") that doesn't exist and wasn't worth building for this. Applies to nominating a leader, resolving a pending nomination, removing a leader, and resolving a member's area-change reassignment — all numbered-list selection from a bounded candidate set instead.

**A member's area change never silently moves them out of their existing group.** Their `group_label` is left untouched so routine top-up runs ignore them; instead a recommendation is computed (reusing the exact same heuristic top-up uses for new members) and a leader has to act on it via the same numbered-list pattern, approving the recommendation or overriding it. All three affected parties get notified: the member (once resolved, naming their new group and leader), the leader who resolves it, and the *old* group's leader (their roster just shrank).

**Read-only questions about groups/leaders/membership use LLM tool-calling instead of one hand-coded intent per question shape; actions that change data stay on fixed intents with confirmation.** Adding a new intent for every phrasing of every possible question doesn't scale, and was already showing strain (a growing pile of narrow "view X" intents). The distinction that matters: getting a tool choice wrong for a *question* just produces an unhelpful answer; getting an *action* wrong changes something for someone without proper confirmation. So allocation, leader nomination, and reassignment resolution all remain fixed, confirmed intents — `group_query` handles open-ended questions by giving the LLM a small toolbox (own group; own led group if a group leader; org-wide listing/roster tools if an exec leader) scoped dynamically to the asker's actual role, never trusted from the LLM's own request. Verified the underlying Groq model supports tool-calling before building around it, and verified permission scoping empirically (a regular member's org-wide question is correctly declined because the tool is never offered to them, not just prompted against).

**`group_query` bugs found via a real live phone test, all fixed (2026-09-24).** A genuinely useful stress test — five distinct real bugs surfaced from actual use that hadn't shown up in earlier scripted testing: (1) area name matching was exact-string, case-sensitive, and the model wasn't reliably preserving the user's casing when building tool calls, so "bomas" (lowercase) sometimes returned zero groups; fixed by making the database match case-insensitively and by substring. (2) No partial/alias matching at all — "internal" for "Internal Hostels" failed outright; fixed by telling the model the canonical area list up front and having it resolve loose names before calling a tool. (3) Looking up one specific group required knowing the exact "Area #N" label, which no user has reason to know; added a loose-identifier resolver that falls back to "this area's only group" when unambiguous. (4) The model conflated "who leads the group I belong to" with "am I a leader" — confirmed live, a real group leader was told "No, you're not a leader." Fixed by renaming the ambiguous field and being explicit in both the tool descriptions and system prompt about the distinction. (5) Some phrasings intermittently landed on the older, less-capable `view_groups` intent instead of `group_query`, silently losing detail (a request for full lists "with members" came back as counts-only). Retired `view_groups` entirely rather than trying to out-word the ambiguity — its behavior is fully subsumed by `group_query`'s `list_groups` tool, so nothing was lost. All five re-verified against real data after fixing.

**Deferred, explicitly agreed to tackle separately: the whole bot has no conversation memory, at any level.** Every message — both the top-level intent classifier and each `group_query` call — is handled as if it's the first thing ever said, with zero memory of the immediately preceding turn. This isn't a `group_query`-specific bug; it's a pre-existing property of the whole architecture that `group_query` just made far more *visible*, because its open-ended phrasing invites natural follow-ups ("then what am I", a bare corrected spelling after a failed lookup) in a way the old rigid fixed-intent flows didn't. Deliberately not patched in a hurry — deciding how much history to carry, for how long, and at what per-message token cost needs its own proper design pass, not a bolt-on.

**Conversation memory — DONE (2026-09-24, pushed as `28071e6`), `app/models/conversation_history.py`.** Follow-up to the deferred item above, designed properly rather than bolted on. A bounded rolling log (last 10 messages, both sides) per `whatsapp_id`, wired into two places: `classify_intent` (so short/ambiguous messages classify correctly using recent context) and `group_query`'s tool-calling loop (same, for interpreting a follow-up in context). Deliberately scoped to only the general, freely-classified chat path — the other conversational flows (registration, pending-action confirmations, leader nomination, area change) already have their own dedicated step-tracking and don't go through ambiguous LLM classification mid-flow, so they don't need this.
  - **Window size: 10 messages (~5 exchanges).** Enough for a short clarification chain without letting every classification call grow unboundedly as a conversation gets longer; Groq's fast/free-tier-generous enough that this is a cheap default either way.
  - **Consent**: this is the first table in the project to store raw message text rather than structured/extracted fields, worth naming explicitly given how central informed consent is to this project's design. Decided it's covered under the existing `data_consent` — it's operational data in service of the same already-consented conversation, not a new purpose, never leader-visible — rather than needing its own consent prompt. But `withdraw_data_consent` now also purges it, so withdrawing genuinely means no trace is kept.
  - **Logging order matters**: the current exchange is logged AFTER the handler produces a reply, not before -- otherwise a call's own history fetch would double up with the message it's currently processing.
  - Verified live: "and number 3?" (meaningless without context) correctly resolved to "Bomas #3" right after asking about "Bomas number 2", using only the logged history; consent withdrawal correctly purged a test history to zero rows.

**`group_query` round 2 — multi-group-area handling, all fixed (2026-09-24).** A follow-up live test (this time run from a written 14-question script covering permission tiers, area matching, loose lookup, full-listing formatting, and conversation memory) surfaced five more real bugs, all one underlying pattern: no consistent handling for a request that spans *every* group in an area rather than one specific group. (1) "What about internal hostels" (a follow-up after a different area's question succeeded) fell to a generic apology instead of resolving to that area. (2) "I want the full details of the groups in bomas" — same generic fallback. (3) "Names of the members of all groups in bomas" — no tool existed for "every roster in an area in one call", so the model had nothing reliable to chain. (4) Replying "All of them" to the bot's own "which group did you mean?" clarifying question wasn't recognized as answering that question. (5) "What of the members" (a follow-up after a groups-summary list) also fell to the generic fallback. Fixed by: adding a new `get_area_rosters` tool (every group in an area, each with its full roster, in one call — instead of the model having to improvise chaining several single-group lookups); substantially expanding `_SYSTEM_PROMPT` with explicit decision rules for area-wide requests (summary → `list_groups`, names → `get_area_rosters`, "which one?" replied with "all of them" → resolve to whichever the original ask wanted); raising `_MAX_TOOL_ROUNDS` 3→4 (multi-group answers sometimes need an extra round); replacing the bare apology with a more actionable `_FALLBACK_MESSAGE` that invites a narrower retry. All five re-verified against real data after fixing.

**Found and fixed alongside round 2: `classify_intent` could misroute an in-topic follow-up to `general_question`, bypassing `group_query` entirely.** While re-testing bug (1) above, "What about internal hostels" started returning the *unrelated* `general_question` stub text — a different, worse failure than the original bug, since the message never even reached `group_query`'s tools/prompt fixes. Root cause: `general_question`'s definition was generic enough to compete with `group_query` for any message that reads fine as a standalone topic (hostel facilities, in this case) even when the immediately preceding exchange was clearly about Bible study groups. Adding a generic "use history to resolve short/vague follow-ups" instruction wasn't enough — the model still needs a concrete example naming this exact ambiguity to reliably override it. Fixed by narrowing `general_question`'s definition to explicitly exclude group topics, and adding a worked example to the classifier's system prompt (`_build_system_prompt` in `intent_router.py`) showing precisely this "named a plausible standalone topic, but history says it's actually a group follow-up" case. Also surfaced a testing-hygiene issue along the way: the shared manual-test `whatsapp_id`'s conversation history was carrying stale rows across unrelated test rounds from earlier sessions, which was polluting classification context for new tests — worth clearing (`clear_conversation_history`) before each fresh manual test run, not just relying on the 10-message window to age it out.
