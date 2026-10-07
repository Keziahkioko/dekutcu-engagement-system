"""
demo/seed_demo_history.py

Fills the DEMO database (a separate Neon branch -- see demo_safety.py) with
a believable 12-week semester of history, so every page of the reports
website has something real-looking to show when presenting. From the
project folder:

    venv\\Scripts\\python demo\\seed_demo_history.py

Re-run it on the morning of a presentation: every date is relative to the
day it runs, so "last 7 days" always has data.

SYNTHETIC. The patterns below (a dip in CAT weeks, the bandit drifting
toward the strategy that suits each reason, adaptive members recovering
somewhat more often than the control group) are WRITTEN IN on purpose to
illustrate what the charts show. They are not findings and must never be
reported as Objective 3 results.

How it's built:
  - Safety: connects ONLY via demo_database_url(), which refuses unless
    DEMO_DATABASE_URL is a different server from the real DATABASE_URL.
  - Repeatable: a fixed random seed, and it clears the previous demo
    history first, so every run gives the same semester (dated to today).
  - Consistent: each member is simulated session by session. An absence,
    the reason, the strategy the bandit picks, and whether the member comes
    back afterwards are generated together -- so the Evaluation page's
    "returned next session" and "recovered within 2 sessions" figures are
    computed by the real reporting code from coherent data, not typed in.
  - Members: the 199 TEST members already copied into the branch. In the
    demo copy only, their names lose the "TEST " prefix, registration
    dates are spread over the semester, one member per group becomes its
    group leader, five hold exec offices, ~15% are in the control group.
"""

import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from demo.demo_safety import demo_database_url

SEED = 2026
WEEKS = 12
rng = random.Random(SEED)

NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)   # stored as naive UTC, like the app
TODAY = NOW.date()
START = TODAY - timedelta(weeks=WEEKS)
CAT_WEEKS = {6, 7}   # 0-based weeks of the semester with CATs

ARMS = ["reminder", "empathetic_checkin", "barrier_support", "peer_connection"]
# Which strategy suits which reason (illustrative -- this is what the bandit "discovers").
BEST_ARM = {
    "scheduling_conflict": "reminder",
    "health": "empathetic_checkin",
    "personal_difficulty": "empathetic_checkin",
    "logistical_barrier": "barrier_support",
    "disengagement": "peer_connection",
    "unclassified": "empathetic_checkin",
}
FELLOWSHIPS = {0: "monday_fellowship", 2: "wednesday_prayers", 3: "thursday_fellowship", 4: "friday_fellowship"}
FELLOWSHIP_SHARE = {"monday_fellowship": 0.30, "wednesday_prayers": 0.40,
                    "thursday_fellowship": 0.25, "friday_fellowship": 0.45}

REASONS = {   # category -> phrases members might write (stored as reason_raw; never shown in reports)
    "scheduling_conflict": ["I had a CAT the next morning", "class ran late", "group assignment meeting",
                            "lab session was moved to that evening", "I was revising for exams"],
    "health": ["I was sick", "had a bad headache", "went to the clinic", "I had flu"],
    "personal_difficulty": ["things have been hard at home", "I haven't been feeling like myself lately"],
    "logistical_barrier": ["it was raining heavily and I live far", "no fare to come back to campus",
                           "the venue is too far from my hostel"],
    "disengagement": ["I just didn't feel like coming", "I forgot", "wasn't really interested this week"],
    "unclassified": ["something came up", "sorry"],
}

FEEDBACK = {   # theme -> replies (anonymous feedback IS shown to leaders, so these read like real replies)
    "feedback": ["The study on Philippians was really encouraging", "Loved the discussion tonight",
                 "Worship was powerful today", "The session felt a bit rushed", "Good teaching, very practical",
                 "I liked that everyone got a chance to share"],
    "question": ["When will we start the new book study?", "Can we get the study notes in advance?",
                 "Is there a fellowship for first years?", "Who do I talk to about joining the praise team?"],
    "recommendation": ["Maybe we could start 15 minutes earlier", "Please share the passage in the group before the session",
                       "We should have more time for prayer", "Could we do a joint session with another group sometime?",
                       "Snacks after would help people stay and talk"],
    "challenge": ["The venue is too far for those of us in Nyaribo", "It clashes with my lab on some weeks",
                  "It's hard to come when it rains", "The room was too small for everyone"],
}

GENERAL_QUESTIONS = [   # answered from the constitution
    "What does DeKUTCU believe about the Bible?", "How are exec members elected?", "Who can become a member of the CU?",
    "What is the doctrinal basis of the CU?", "How often does the exec committee meet?", "What are the aims of the CU?",
    "Can a member be removed from the CU?", "What does the CU believe about salvation?", "What is the role of the chairperson?",
    "How is the constitution amended?", "What are the ministries in the CU?", "What does the CU believe about the Holy Spirit?",
]
NOT_COVERED = [   # asked repeatedly and not in the constitution -> the "material worth adding" list
    ("When is the next AGM?", 4), ("How much is the mission trip this semester?", 3),
    ("How do I join the praise and worship team?", 3), ("What time does Sunday service start?", 2),
    ("Is there a Bible study for first years only?", 2), ("Where can I get a study Bible on campus?", 1),
]
SECONDARY = ["Does the CU believe in speaking in tongues?", "Should baptism be by immersion?",
             "What does the CU say about the end times?", "Is it okay for Christians to drink alcohol?"]
PASTORAL = ["How do I know God's will for my life?", "How do I deal with doubt?",
            "How do I forgive someone who hurt me?", "How can I grow in prayer?"]

MEMBER_QUESTIONS = [
    ("Can we have a session on managing money as students?", "Yes -- the Finance Secretary is planning one next month."),
    ("Is there a way to get involved in missions this semester?", "Yes! Mission week registration opens soon; watch for the announcement."),
    ("Can first years join the exec?", "Not yet -- exec positions are open from second year, but ministries welcome everyone."),
    ("Who leads the Friday fellowship?", "The Discipleship Ministry coordinates it with a different speaker each week."),
    ("Can the Bible study start earlier during exams?", None),
    ("Is there a Christian counsellor on campus we can talk to?", None),
]

EVENTS = [   # (days from today, title, type, location, share of members who reply)
    (-70, "Freshers' Welcome Fellowship", "tracked", "Main Hall", 0.45),
    (-49, "Prayer and Fasting Day", "broadcast", "Chapel", 0),
    (-35, "Worship Night", "tracked", "Main Hall", 0.40),
    (-14, "Hike to Kirimara Hill", "tracked", "Main Gate", 0.30),
    (5, "Mission Week Briefing", "tracked", "Lecture Hall 3", 0.35),
    (12, "Joint Fellowship with Karatina University CU", "tracked", "Main Hall", 0.30),
    (19, "End of Semester Worship Night", "tracked", "Main Hall", 0.25),
    (26, "Semester Break Prayer Meeting", "broadcast", "Chapel", 0),
]

EXEC_OFFICES = ["Chairperson", "First Vice Chairperson", "Second Vice Chairperson", "Secretary",
                "Social Welfare Ministry Director"]

# Cleared before each run. pending_reason_capture and pending_feedback point at absences and
# feedback_requests (half-finished conversations), so they're cleared with them -- listed by name
# rather than TRUNCATE ... CASCADE, which would silently clear any table that happens to link here.
HISTORY_TABLES = ["event_rsvps", "events", "escalations", "escalation_cases", "pending_reason_capture", "absences",
                  "attendance_markings", "fellowship_checkins", "checkin_broadcasts", "member_questions",
                  "pending_feedback", "feedback_requests", "rag_queries",
                  # Stage 14 (added 2026-10-07): the semester's study guide -- see seed_study_guide().
                  "guide_purchases", "guide_batches", "study_guides", "guide_coordinator"]


def at(day, hour, minute=0):
    """A naive-UTC timestamp on `day` at Nairobi local `hour` (UTC+3)."""
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=hour - 3, minutes=minute)


def week_of(day):
    return (day - START).days // 7


def sessions_on(weekday):
    """Every date of the semester falling on `weekday`, before today (today's session hasn't been checked in yet)."""
    d = START + timedelta(days=(weekday - START.weekday()) % 7)
    out = []
    while d < TODAY:
        out.append(d)
        d += timedelta(days=7)
    return out


def choose_arm(category, week, control):
    """Control members always get the plain reminder. Otherwise: roughly even early on,
    increasingly the strategy that suits the reason as the policy learns."""
    if control:
        return "reminder"
    p_best = 0.25 + 0.65 * min(1.0, week / 6)   # learns over about the first 6 weeks
    best = BEST_ARM[category]
    if rng.random() < p_best:
        return best
    return rng.choice([a for a in ARMS if a != best])


def reason_category(week):
    weights = {"scheduling_conflict": 30, "health": 18, "personal_difficulty": 6,
               "logistical_barrier": 16, "disengagement": 14, "unclassified": 6}
    if week in CAT_WEEKS:
        weights["scheduling_conflict"] = 70   # CAT season
    cats = list(weights)
    return rng.choices(cats, weights=[weights[c] for c in cats])[0]


def simulate(member, activity, dates, base_p):
    """
    One member's attendance at one weekly activity. Returns (present_dates, absences).
    One miss makes the next more likely: straight after an absence, a member's own
    chance of coming back drops by about a third -- which is why the follow-up matters.
    A follow-up only restores that chance when its strategy suits the reason.
    Lapsing members stop attending over the last 3 weeks.
    """
    present, absences = [], []
    boost = 0.0
    missed_previous = False
    for d in dates:
        w = week_of(d)
        p = base_p * (0.8 if w in CAT_WEEKS else 1.0) * (0.7 if missed_previous else 1.0) + boost
        if member["lapsing"] and w >= WEEKS - 3:
            p = 0.05
        boost = 0.0
        if rng.random() < min(p, 0.97):
            present.append(d)
            missed_previous = False
            continue
        absence = {"reg": member["reg"], "activity": activity, "date": d, "week": w,
                   "category": None, "raw": None, "arm": None, "control": member["control"],
                   "distress": False, "reward": None, "recovered": None,
                   "miss_bucket": "2+" if missed_previous else "1"}
        missed_previous = True
        # Most members reply with a reason -- but opted-out members are never asked (no follow-up consent).
        if not member["opted_out"] and rng.random() < 0.8:
            cat = reason_category(w)
            absence.update(category=cat, raw=rng.choice(REASONS[cat]))
            absence["arm"] = choose_arm(cat, w, member["control"])
            if cat == "personal_difficulty" and rng.random() < 0.3:
                absence["distress"] = True
            suits = absence["arm"] == BEST_ARM[cat]
            boost = 0.35 if suits else 0.0   # only the strategy that suits the reason makes a real difference
        absences.append(absence)
    # Outcomes, exactly as the real reward jobs define them:
    #   reward    -- came back at the very next session (unknown until it has happened)
    #   recovered -- came back at either of the next two (unknown until both have happened, unless already back)
    present_set = set(present)
    for a in absences:
        if a["arm"] is None:
            continue
        nxt = [d for d in dates if d > a["date"]][:2]
        if nxt:
            a["reward"] = nxt[0] in present_set
        if any(d in present_set for d in nxt):
            a["recovered"] = True
        elif len(nxt) == 2:
            a["recovered"] = False
    return present, absences


def main():
    conn = psycopg2.connect(demo_database_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    def many(sql, rows):
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)

    # --- 1. Clear previous demo history (demo database only -- guaranteed by demo_database_url) ---
    cur.execute(f"TRUNCATE {', '.join(HISTORY_TABLES)} RESTART IDENTITY")

    # --- 2. Shape the members (demo copy only) ---
    cur.execute("""SELECT reg_number, name, group_label, leads_group_label, is_leader FROM members
                   ORDER BY reg_number""")
    everyone = cur.fetchall()
    real = [m for m in everyone if not m["reg_number"].startswith("TEST-")]
    tests = [m for m in everyone if m["reg_number"].startswith("TEST-")]
    viewer = next((m for m in real if m["is_leader"]), None)
    if not viewer:
        sys.exit("Stopped: no real exec leader in the demo database.")

    cur.execute("""UPDATE members SET exec_office = NULL, leads_group_label = NULL, is_leader = FALSE
                   WHERE reg_number LIKE 'TEST-%'""")
    members = []
    for m in tests:
        weeks_ago = rng.choices(range(WEEKS + 1), weights=[1] * (WEEKS - 2) + [6, 10, 12])[0]   # onboarding surge early on
        registered = at(TODAY - timedelta(weeks=weeks_ago, days=rng.randint(0, 6)), rng.randint(8, 21))
        registered = min(registered, NOW - timedelta(hours=1))
        control = False   # assigned below, stratified
        opted_out = rng.random() < 0.04
        name = m["name"][5:] if m["name"].startswith("TEST ") else m["name"]
        cur.execute("""UPDATE members SET name = %s, registered_at = %s,
                       followup_consent = %s, data_consent = TRUE WHERE reg_number = %s""",
                    (name, registered, not opted_out, m["reg_number"]))
        members.append({"reg": m["reg_number"], "name": name, "group": m["group_label"], "control": control,
                        "registered": registered.date(), "opted_out": opted_out,
                        "lapsing": rng.random() < 0.05, "p": min(0.97, max(0.35, rng.gauss(0.80, 0.14)))})

    # Control group (~15%), STRATIFIED: sort members by how regularly they attend and pick one
    # at random from each block of 7, so control and adaptive start out equally committed.
    # With plain random assignment and only ~30 control members, luck in who lands in control
    # outweighed the whole effect in testing (control happened to be the keener attenders).
    ranked = sorted(members, key=lambda m: m["p"])
    for i in range(0, len(ranked), 7):
        rng.choice(ranked[i:i + 7])["control"] = True
    for m in members:
        cur.execute("UPDATE members SET bandit_control_group = %s WHERE reg_number = %s", (m["control"], m["reg"]))

    # Group leaders: one member per group (Bomas #1 is led by the real leader already).
    groups = sorted({m["group"] for m in members if m["group"]})
    leader_of = {g: viewer["reg_number"] for g in groups if g == viewer.get("leads_group_label")}
    for g in groups:
        if g in leader_of:
            continue
        pick = next(m for m in members if m["group"] == g)
        leader_of[g] = pick["reg"]
        pick["lapsing"] = False
        cur.execute("UPDATE members SET leads_group_label = %s WHERE reg_number = %s", (g, pick["reg"]))
    # Exec offices: five members, never group leaders.
    execs = []
    for office in EXEC_OFFICES:
        pick = rng.choice([m for m in members if m["reg"] not in leader_of.values() and m["reg"] not in execs])
        execs.append(pick["reg"])
        cur.execute("UPDATE members SET is_leader = TRUE, exec_office = %s WHERE reg_number = %s", (office, pick["reg"]))
    # A withdrawn member keeps their row but leaves everything else (data_consent FALSE).
    withdrawn = rng.sample([m for m in members if m["reg"] not in leader_of.values() and m["reg"] not in execs], 2)
    for m in withdrawn:
        cur.execute("UPDATE members SET data_consent = FALSE WHERE reg_number = %s", (m["reg"],))
        members.remove(m)

    # --- 3. Bible Study (Tuesdays): attendance markings + absences ---
    all_absences, feedback_rows, markings, checkins, broadcasts = [], [], [], [], []
    bs_dates = sessions_on(1)
    presence = {}   # (reg, activity) -> present dates, for feedback requests
    for g in groups:
        roster = [m for m in members if m["group"] == g]
        per_member = {}
        for m in roster:
            dates = [d for d in bs_dates if d >= m["registered"]]
            per_member[m["reg"]] = simulate(m, "bible_study", dates, m["p"])
        for d in bs_dates:
            on_roster = [m for m in roster if m["registered"] <= d]
            if not on_roster:
                continue
            forgot = rng.random() < 0.06 or (d == bs_dates[-1] and g in ("Kahawa #1", "Embassy #1"))
            if forgot:
                continue   # a leader who didn't mark that week -> no record, no absences (the system never guesses)
            absent = [a for m in on_roster for a in per_member[m["reg"]][1] if a["date"] == d]
            markings.append((g, d, len(on_roster), len(absent), leader_of[g], at(d, 20, rng.randint(0, 50))))
            all_absences.extend(absent)
            for m in on_roster:
                if d in per_member[m["reg"]][0]:
                    presence.setdefault((m["reg"], "bible_study"), []).append(d)

    # --- 4. Fellowships: check-in broadcasts, "present" replies, absences for regulars ---
    for weekday, activity in FELLOWSHIPS.items():
        dates = sessions_on(weekday)
        attendees = [m for m in members if not m["opted_out"] and rng.random() < FELLOWSHIP_SHARE[activity]]
        leader_days = set(rng.sample(dates, k=len(dates) // 3))   # about a third sent live by a leader
        for d in dates:
            broadcasts.append((activity, d, "leader" if d in leader_days else "scheduled",
                               at(d, 19 if d in leader_days else 21, rng.randint(0, 20))))
        for m in attendees:
            mdates = [d for d in dates if d >= m["registered"]]
            present, absences = simulate(m, activity, mdates, min(0.95, m["p"] + 0.02))
            for d in present:
                checkins.append((m["reg"], activity, d, at(d, 21, rng.randint(1, 55))))
                presence.setdefault((m["reg"], activity), []).append(d)
            all_absences.extend(absences)

    # --- 5. Feedback requests (to members who were present) + a few unprompted ---
    for (reg, activity), dates in presence.items():
        for d in dates:
            if activity == "bible_study":
                trigger, sent = "bible_study", at(d, 20, 55)
            else:
                led = any(b[0] == activity and b[1] == d and b[2] == "leader" for b in broadcasts)
                trigger, sent = ("leader", at(d, 19, 30)) if led else ("scheduled", at(d, 21, 30))
            w = week_of(d)
            if rng.random() > 0.35:   # feedback isn't asked after every single session for everyone
                continue
            respond_p = 0.50 - 0.012 * w   # novelty wears off a little across the semester
            responded, text, severity, theme = None, None, None, None
            reply_at = sent + timedelta(minutes=rng.randint(3, 180))
            if rng.random() < respond_p and reply_at < NOW:
                responded = reply_at
                theme = rng.choices(list(FEEDBACK), weights=[45, 15, 25, 15])[0]
                text, severity = rng.choice(FEEDBACK[theme]), "none"
                if responded > NOW - timedelta(hours=20):
                    theme = None   # the daily 2pm sorting hasn't run on these yet
            feedback_rows.append((reg, activity, d, trigger, sent, responded, text, severity, theme))
    for reg, when in [(rng.choice(members)["reg"], NOW - timedelta(days=k, hours=rng.randint(1, 9))) for k in (3, 9, 16, 30, 44)]:
        theme = rng.choice(["feedback", "recommendation"])
        feedback_rows.append((reg, "general", when.date(), "unprompted", None, when, rng.choice(FEEDBACK[theme]), "none", theme))
    distress_texts = ["I've been really struggling and don't know who to talk to",
                      "honestly things are very heavy for me right now"]
    for k, t in zip((12, 40), distress_texts):   # counted on the Care page, never shown
        when = at(TODAY - timedelta(days=k), 21, 40)
        feedback_rows.append((rng.choice(members)["reg"], "bible_study", when.date(), "bible_study",
                              when - timedelta(minutes=45), when, t, "distress", None))

    # --- 6. Escalation cases ---
    exec_regs = execs + [viewer["reg_number"]]
    chair_and_vices = execs[:3]
    cases = [   # (days ago, hours, trigger, urgency, minutes to claim or None if open, reminded, backstopped, viewer notified)
        (78, 14, "needs_support", "normal", 35, False, False, False),
        (66, 19, "reason_capture", "normal", 150, True, False, False),
        (57, 21, "feedback", "normal", 25, False, False, True),
        (50, 22, "needs_support", "acute", 12, False, False, True),
        (44, 13, "request_human", "normal", 55, False, False, False),
        (37, 20, "rag_question", "normal", 410, True, True, False),
        (29, 16, "needs_support", "normal", 18, False, False, True),
        (22, 18, "reason_capture", "normal", 70, False, False, False),
        (15, 21, "rag_question", "normal", 140, True, False, True),
        (8, 23, "needs_support", "acute", 9, False, False, True),
        (4, 17, "request_human", "normal", 45, False, False, False),
    ]
    open_cases = [(3.2, "needs_support", True, True), (0.7, "rag_question", False, False)]   # (hours ago, trigger, reminded, viewer notified)
    subjects = rng.sample([m for m in members if m["reg"] not in leader_of.values() and m["reg"] not in execs],
                          len(cases) + len(open_cases))
    esc_log = []
    for i, (days_ago, hour, trig, urgency, mins, reminded, backstop, viewer_notified) in enumerate(cases):
        created = at(TODAY - timedelta(days=days_ago), hour, rng.randint(0, 59))
        subject = subjects[i]
        notified = exec_regs if urgency == "acute" else [leader_of[subject["group"]]]
        if viewer_notified and viewer["reg_number"] not in notified:
            notified = notified + [viewer["reg_number"]]
        if backstop:
            notified = notified + chair_and_vices
        claimer = chair_and_vices[0] if backstop else rng.choice(notified)
        claimed = created + timedelta(minutes=mins)
        reminded_at = created + timedelta(minutes=30 if urgency == "acute" else 120) if reminded else None
        backstop_at = created + timedelta(hours=2 if urgency == "acute" else 6) if backstop else None
        cur.execute("""INSERT INTO escalation_cases (reg_number, trigger_type, context_text, created_at,
                       claimed_by_reg_number, claimed_at, urgency, reminded_at, backstop_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (subject["reg"], trig, "(synthetic demo case)", created, claimer, claimed, urgency, reminded_at, backstop_at))
        case_id = cur.fetchone()["id"]
        esc_log += [(subject["reg"], trig, "(synthetic demo case)", n, created, case_id) for n in dict.fromkeys(notified)]
    for j, (hours_ago, trig, reminded, viewer_notified) in enumerate(open_cases):
        created = NOW - timedelta(hours=hours_ago)
        subject = subjects[len(cases) + j]
        notified = [leader_of[subject["group"]]] + ([viewer["reg_number"]] if viewer_notified else [])
        cur.execute("""INSERT INTO escalation_cases (reg_number, trigger_type, context_text, created_at, urgency, reminded_at)
                       VALUES (%s, %s, %s, %s, 'normal', %s) RETURNING id""",
                    (subject["reg"], trig, "(synthetic demo case)", created,
                     created + timedelta(hours=2) if reminded else None))
        case_id = cur.fetchone()["id"]
        esc_log += [(subject["reg"], trig, "(synthetic demo case)", n, created, case_id) for n in dict.fromkeys(notified)]

    # --- 7. Companion questions ---
    rag_rows = []
    def rag(kind, question, outcome, severity="none", days_ago=None):
        when = NOW - timedelta(days=days_ago if days_ago is not None else rng.uniform(0.2, WEEKS * 7 - 1),
                               minutes=rng.randint(0, 600))
        rag_rows.append((rng.choice(members)["reg"], question, kind, severity, json.dumps([]), outcome,
                         None if outcome != "answered" else "(synthetic demo answer)", json.dumps([]), 0,
                         rng.randint(900, 2600), when))
    for _ in range(48):
        rag("general", rng.choice(GENERAL_QUESTIONS), "answered")
    for q, times in NOT_COVERED:
        for _ in range(times):
            rag("general", q, "not_covered", days_ago=rng.uniform(0.3, 40))
    for q in SECONDARY:
        rag("general", q, "secondary_issue")
    for q in PASTORAL * 2:
        rag("pastoral", q, "answered")
    rag("pastoral", "(synthetic pastoral question)", "escalated", severity="distress")
    for q in ["When is the next Bible study?", "Can I bring a friend to fellowship?"]:
        rag("feedback_question", q, "answered")
    rag("general", "What does the constitution say about ministries?", "error")

    # --- 8. Members' questions relayed to leaders ---
    mq_rows = []
    for i, (q, answer) in enumerate(MEMBER_QUESTIONS):
        created = NOW - timedelta(days=(60 - 11 * i) if answer else (3 if i == 4 else 1), hours=rng.randint(0, 8))
        answered_by = rng.choice(exec_regs) if answer else None
        mq_rows.append((rng.choice(members)["reg"], q, created, answered_by, answer,
                        created + timedelta(hours=rng.randint(2, 30)) if answer else None))

    # --- 9. Events + RSVPs ---
    rsvp_rows = []
    for offset, title, etype, location, share in EVENTS:
        day = TODAY + timedelta(days=offset)
        cur.execute("""INSERT INTO events (event_type, title, event_date, event_time, location, description, created_by, created_at)
                       VALUES (%s, %s, %s, '6:00 PM', %s, %s, %s, %s) RETURNING id""",
                    (etype, title, day, location, "(synthetic demo event)", rng.choice(exec_regs),
                     min(NOW, at(day - timedelta(days=14), 10))))
        event_id = cur.fetchone()["id"]
        if etype == "tracked":
            for m in rng.sample(members, int(len(members) * share)):
                resp = rng.choices(["yes", "maybe", "no"], weights=[60, 25, 15])[0]
                rsvp_rows.append((event_id, f"demo-{m['reg']}", resp, min(NOW, at(day - timedelta(days=rng.randint(1, 10)), 12))))

    # --- 10. Write it all ---
    many("""INSERT INTO attendance_markings (group_label, activity_date, roster_size, absent_count, marked_by_reg_number, marked_at)
            VALUES (%s, %s, %s, %s, %s, %s)""", markings)
    many("""INSERT INTO checkin_broadcasts (activity_type, checkin_date, triggered_by, sent_at) VALUES (%s, %s, %s, %s)""", broadcasts)
    many("""INSERT INTO fellowship_checkins (reg_number, activity_type, checkin_date, created_at) VALUES (%s, %s, %s, %s)""", checkins)
    many("""INSERT INTO absences (reg_number, activity_type, activity_date, reason_raw, reason_category, shows_distress,
            distress_level, created_at, context_key, chosen_arm, reward, is_control, recovered_within_2)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
         [(a["reg"], a["activity"], a["date"], a["raw"], a["category"], a["distress"],
           "distress" if a["distress"] else "none", at(a["date"], 21, 45),
           f"{a['category']}|{a['miss_bucket']}" if a["arm"] else None, a["arm"],
           a.get("reward"), a["control"] if a["arm"] else None, a.get("recovered"))
          for a in sorted(all_absences, key=lambda a: a["date"])])
    many("""INSERT INTO feedback_requests (reg_number, activity_type, activity_date, trigger, sent_at, responded_at,
            response_text, severity, theme) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""", feedback_rows)
    many("""INSERT INTO escalations (reg_number, trigger_type, context_text, notified_leader_reg_number, created_at, case_id)
            VALUES (%s, %s, %s, %s, %s, %s)""", esc_log)
    many("""INSERT INTO rag_queries (reg_number, question, kind, severity, retrieved, outcome, answer, cited,
            invalid_citations, tokens_used, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", rag_rows)
    many("""INSERT INTO member_questions (reg_number, question, created_at, answered_by_reg_number, answer_text, answered_at)
            VALUES (%s, %s, %s, %s, %s, %s)""", mq_rows)
    many("""INSERT INTO event_rsvps (event_id, whatsapp_id, response, responded_at) VALUES (%s, %s, %s, %s)""", rsvp_rows)
    guide_counts = seed_study_guide(cur, members, leader_of, execs)
    conn.commit()

    print(f"Demo semester seeded: {START} to {TODAY} ({WEEKS} weeks), seed {SEED}")
    for label, n in [("members shaped", len(members)), ("group leaders", len(leader_of)), ("exec offices", len(execs)),
                     ("Bible Study markings", len(markings)), ("fellowship check-in days", len(broadcasts)),
                     ("fellowship 'present' replies", len(checkins)), ("absences", len(all_absences)),
                     ("feedback requests", len(feedback_rows)), ("escalation cases", len(cases) + len(open_cases)),
                     ("companion questions", len(rag_rows)), ("members' questions", len(mq_rows)),
                     ("events", len(EVENTS)), ("RSVPs", len(rsvp_rows))] + guide_counts:
        print(f"  {label:30} {n}")
    cur.close()
    conn.close()


def seed_study_guide(cur, members, leader_of, execs):
    """
    Stage 14 (added 2026-10-07, so the guides part of the reports website has something to show): this
    semester's study guide, KES 70, through the whole flow -- a Discipleship Ministry Director, the Guides
    Coordinator they appointed, batches given to group leaders and confirmed by them (one not yet), members
    paying over M-Pesa (codes start "DEMO", clearly fake), guides handed over and confirmed by members (some
    not yet, one disputed), two members who paid twice (refunds), one group short of copies, and a few
    members with no group leader who collect from the Coordinator.
    Only THIS semester's guide: every demo member registered during this semester, so nobody could have
    bought last semester's. Uses its own random generator, so adding it changes nothing else in the semester.
    """
    g_rng = random.Random(SEED + 14)
    taken = set(leader_of.values()) | set(execs)
    free = [m for m in members if m["reg"] not in taken]
    director, coordinator = g_rng.sample(free, 2)
    cur.execute("UPDATE members SET is_leader = TRUE, exec_office = 'Discipleship Ministry Director' WHERE reg_number = %s",
                (director["reg"],))
    started = at(START + timedelta(weeks=1), 10)
    cur.execute("""INSERT INTO guide_coordinator (id, reg_number, appointed_by_reg_number, appointed_at)
                   VALUES (TRUE, %s, %s, %s)""", (coordinator["reg"], director["reg"], started - timedelta(days=2)))
    cur.execute("""INSERT INTO study_guides (title, price_kes, is_current, started_by_reg_number, started_at)
                   VALUES ('Romans: Grace Alone', 70, TRUE, %s, %s) RETURNING id""", (director["reg"], started))
    guide_id = cur.fetchone()["id"]

    groups = sorted(leader_of)
    unconfirmed_group, short_group = g_rng.sample(groups, 2)   # a batch not yet confirmed; a group short of copies
    batches, purchases, receipts = [], [], [0]

    def receipt():
        receipts[0] += 1
        return f"DEMO{receipts[0]:06d}"

    def pay(m, paid_at, status="paid"):
        if "registered" in m:   # nobody pays before they registered, or in the future
            joined = datetime.combine(m["registered"], datetime.min.time()) + timedelta(hours=g_rng.randint(9, 20))
            paid_at = min(max(paid_at, joined), NOW - timedelta(hours=2))
        purchase = {"reg": m["reg"], "status": status, "paid_at": paid_at, "receipt": receipt(),
                    "collected_at": None, "collected_by": None, "receipt_status": None, "answered_at": None}
        purchases.append(purchase)
        return purchase

    def hand_over(p, giver):
        p["collected_at"] = min(p["paid_at"] + timedelta(days=g_rng.randint(1, 8), hours=g_rng.randint(0, 6)), NOW - timedelta(hours=3))
        p["collected_by"] = giver
        roll = g_rng.random()
        if roll < 0.84:
            p["receipt_status"], p["answered_at"] = "confirmed", p["collected_at"] + timedelta(minutes=g_rng.randint(20, 600))
        elif roll < 0.97:
            p["receipt_status"] = "awaiting"       # handed over, the member hasn't answered yet
        else:                                      # "No, I didn't get it" -- the hand-over was reversed
            p["receipt_status"], p["answered_at"] = "disputed", p["collected_at"] + timedelta(hours=2)
            p["collected_at"] = p["collected_by"] = None

    def paid_time():
        days = min(g_rng.expovariate(1 / 12), (NOW - started).days - 1)   # most pay in the first weeks
        return started + timedelta(days=days, hours=g_rng.randint(0, 12))

    for g in groups:
        roster = [m for m in members if m["group"] == g]
        buyers = [m for m in roster if g_rng.random() < 0.62]
        paid = sorted((pay(m, paid_time()) for m in buyers), key=lambda p: p["paid_at"])
        leader = leader_of[g]
        if g == unconfirmed_group:      # given 2 days ago, the leader hasn't replied RECEIVED yet
            given = NOW - timedelta(days=2)
            batches.append((guide_id, leader, max(5, len(paid)), coordinator["reg"], given, "pending", None, None, given + timedelta(hours=24)))
            continue
        copies = max(5, -(-len(paid) // 5) * 5) if g != short_group else max(3, len(paid) - 3)
        given = started + timedelta(days=g_rng.randint(4, 10))
        batches.append((guide_id, leader, copies, coordinator["reg"], given, "confirmed", copies, given + timedelta(hours=g_rng.randint(2, 20)), None))
        for p in paid[:copies]:
            if p["paid_at"] < NOW - timedelta(days=3) and g_rng.random() < 0.85:
                hand_over(p, leader)

    # Members with no Bible Study group collect from the Guides Coordinator.
    for m in [m for m in members if not m["group"] and m["reg"] != coordinator["reg"]]:
        if g_rng.random() < 0.4:
            p = pay(m, paid_time())
            if g_rng.random() < 0.5 and p["paid_at"] < NOW - timedelta(days=3):
                hand_over(p, coordinator["reg"])

    # Two members paid twice (the second M-Pesa prompt was approved too) -> refunds needed.
    for p in g_rng.sample([p for p in purchases if p["status"] == "paid"], 2):
        pay({"reg": p["reg"]}, p["paid_at"] + timedelta(minutes=4), status="duplicate")

    many_rows = [(guide_id, p["reg"], 70, None, p["status"], f"ws_CO_DEMO_{i:05d}", p["receipt"], 0,
                  "The service request is processed successfully.", p["paid_at"] - timedelta(minutes=1), p["paid_at"],
                  p["collected_at"], p["collected_by"], p["receipt_status"], p["answered_at"])
                 for i, p in enumerate(purchases, 1)]
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO guide_purchases (guide_id, reg_number, amount_kes, phone, status, checkout_request_id, mpesa_receipt,
               result_code, result_desc, requested_at, paid_at, collected_at, collected_by_reg_number, receipt_status,
               receipt_answered_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", many_rows)
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO guide_batches (guide_id, leader_reg_number, copies, given_by_reg_number, given_at, status,
               copies_received, confirmed_at, reminded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""", batches)
    paid = [p for p in purchases if p["status"] == "paid"]
    return [("study guide purchases (paid)", len(paid)),
            ("  handed out", sum(p["collected_at"] is not None for p in paid)),
            ("  refunds needed", sum(p["status"] == "duplicate" for p in purchases)),
            ("guide batches", len(batches))]


if __name__ == "__main__":
    main()
