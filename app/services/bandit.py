"""
app/services/bandit.py

Stage 8: the contextual bandit engine (Objective 3) -- tabular
Thompson Sampling over a deliberately small context (absence reason
category x a coarse consecutive-misses bucket), not the full context
originally sketched in the proposal (tenure, time since last
attendance, week of semester). See PROJECT_LOG.md for the full
reasoning: every extra context dimension multiplies the number of
(context, arm) cells that need independent data to learn anything,
and the realistic total number of absence events across a single
-semester deployment is nowhere near enough to support more than
this. Those three dropped dimensions are still fully recoverable
later from data already captured elsewhere (registered_at, absence
dates) -- they're just not fed into the live decision.

Deliberately TABULAR, not the context-generalizing (e.g. logistic)
version of Thompson Sampling -- simpler, fully auditable (a leader
could literally read the alpha/beta counts off the table), and the
honest choice for this data volume rather than a more sophisticated
model this project doesn't have enough data to justify.

Reward is delayed, not immediate: picking a strategy happens the
moment reason capture completes for an absence, but whether it worked
isn't known until the NEXT weekly occurrence of that same specific
activity has passed (see compute_pending_rewards, run daily via
scheduler.py). The recency constraint (never repeat the same arm
twice running for one member+activity) is a selection-time filter,
not a context dimension -- it doesn't multiply the bucket count.
"""

import random
from datetime import timedelta, date

from app.models.absence import (
    count_consecutive_misses,
    get_last_chosen_arm,
    set_chosen_arm,
    set_reward,
    get_absences_awaiting_reward,
    absence_exists_for_date,
)
from app.models.bandit_posterior import get_posterior, update_posterior

ARMS = ["reminder", "empathetic_checkin", "barrier_support", "peer_connection"]

_ARM_MESSAGES = {
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

_REWARD_DELAY_DAYS = 7  # every tracked activity is weekly


def _miss_bucket(consecutive_misses):
    return "1" if consecutive_misses <= 1 else "2+"


def build_context_key(reason_category, consecutive_misses):
    return f"{reason_category}|{_miss_bucket(consecutive_misses)}"


def select_arm_for_absence(absence_id, reg_number, activity_type, activity_date, reason_category):
    """
    Called once reason capture has completed for an absence -- samples
    each arm's Beta posterior for this context, applies the recency
    constraint, records the choice (and the exact context it was
    sampled from, so record_outcome later updates the SAME cell, not
    one recomputed after the fact with possibly-different data), and
    returns the message text to send.
    """
    consecutive_misses = count_consecutive_misses(reg_number, activity_type, activity_date)
    context_key = build_context_key(reason_category, consecutive_misses)

    samples = {arm: random.betavariate(*get_posterior(context_key, arm)) for arm in ARMS}

    last_arm = get_last_chosen_arm(reg_number, activity_type)
    candidates = {a: s for a, s in samples.items() if a != last_arm} if last_arm in samples else samples

    chosen_arm = max(candidates, key=candidates.get)

    set_chosen_arm(absence_id, context_key, chosen_arm)
    return _ARM_MESSAGES[chosen_arm]


def compute_pending_rewards():
    """
    Scheduled daily task -- for every absence with a chosen arm but no
    reward yet, where the next weekly occurrence of that same activity
    has already passed, checks whether a NEW absence exists for that
    same person+activity on that next date. No new absence = presumed
    attended = reward 1. Another absence = reward 0 -- a proxy, not a
    perfect ground truth, but it's exactly the same attendance signal
    Stage 7 already relies on for that activity type (leader-confirmed
    for Bible Study, check-in/silence-inferred for fellowships), not a
    new, separately-trusted mechanism.
    """
    cutoff = date.today() - timedelta(days=_REWARD_DELAY_DAYS)
    for absence in get_absences_awaiting_reward(cutoff):
        next_date = absence["activity_date"] + timedelta(days=_REWARD_DELAY_DAYS)
        reward = not absence_exists_for_date(absence["reg_number"], absence["activity_type"], next_date)
        set_reward(absence["id"], reward)
        update_posterior(absence["context_key"], absence["chosen_arm"], reward)
