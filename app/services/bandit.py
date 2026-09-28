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

Stage 10 (fuller scope) addition -- a static-reminder CONTROL GROUP,
needed for the proposal's own evaluation metric ("attendance recovery
rate vs a static-reminder control"). A small, fixed fraction of
members are held out of adaptive selection entirely: their absences
always get the plain "reminder" arm, and their outcomes are excluded
from update_posterior (see compute_pending_rewards) so a forced
control action never contaminates what the adaptive policy is
learning. Assignment is per-MEMBER, not per-absence -- a member's
group membership is decided once and stays fixed for every absence
they ever have, so their own trajectory is comparable across time
(this also avoids the confusion of the same person alternating
between adaptive and generic treatment from one absence to the next).
Also added: recovered_within_2, a secondary, reporting-only signal
matching the proposal's exact evaluation wording ("recovery within 2
sessions") -- computed over a longer window than the immediate reward,
never fed into any posterior, for every absence regardless of group.

Stage 9 (fuller scope) note: select_arm_for_absence returns just the
chosen ARM NAME now, not message text -- turning that into an actual
message (live, via the LLM, using the member's own stated reason) is
app/services/message_generator.py's job, deliberately kept out of this
file so the bandit's own selection logic stays pure local math with no
API calls at all, as already documented above and in PROJECT_LOG.md.
"""

import random
from datetime import timedelta, date

from app.models.absence import (
    count_consecutive_misses,
    get_last_chosen_arm,
    set_chosen_arm,
    set_reward,
    get_absences_awaiting_reward,
    get_absences_awaiting_recovery,
    set_recovered_within_2,
    absence_exists_for_date,
)
from app.models.bandit_posterior import get_posterior, update_posterior
from app.models.member import get_bandit_control_group, set_bandit_control_group

ARMS = ["reminder", "empathetic_checkin", "barrier_support", "peer_connection"]

# Fraction of members held out as the static-reminder control group --
# see module docstring. Kept small since every member pulled into
# control is also a member NOT contributing training data to the
# already-sparse adaptive policy (see the context-sizing math above).
_CONTROL_GROUP_FRACTION = 0.15

_REWARD_DELAY_DAYS = 7  # every tracked activity is weekly
_RECOVERY_DELAY_DAYS = 14  # two weekly occurrences out


def _miss_bucket(consecutive_misses):
    return "1" if consecutive_misses <= 1 else "2+"


def build_context_key(reason_category, consecutive_misses):
    return f"{reason_category}|{_miss_bucket(consecutive_misses)}"


def _get_or_assign_control_group(reg_number):
    """
    Lazily assigns this member's control-group status the first time
    it's needed, then reuses it forever after -- NOT re-rolled per
    absence, so one member's own results stay comparable across their
    whole history rather than bouncing between adaptive and control
    treatment from one absence to the next.
    """
    is_control = get_bandit_control_group(reg_number)
    if is_control is None:
        is_control = random.random() < _CONTROL_GROUP_FRACTION
        set_bandit_control_group(reg_number, is_control)
    return is_control


def select_arm_for_absence(absence_id, reg_number, activity_type, activity_date, reason_category):
    """
    Called once reason capture has completed for an absence. A
    control-group member (see module docstring) always gets the plain
    "reminder" arm, no Thompson Sampling draw at all. Otherwise,
    samples each arm's Beta posterior for this context, applies the
    recency constraint, records the choice (and the exact context it
    was sampled from, so record_outcome later updates the SAME cell,
    not one recomputed after the fact with possibly-different data).
    Returns just the chosen arm NAME -- see Stage 9 note in the module
    docstring for why turning this into actual message text lives
    elsewhere.
    """
    consecutive_misses = count_consecutive_misses(reg_number, activity_type, activity_date)
    context_key = build_context_key(reason_category, consecutive_misses)
    is_control = _get_or_assign_control_group(reg_number)

    if is_control:
        chosen_arm = "reminder"
    else:
        samples = {arm: random.betavariate(*get_posterior(context_key, arm)) for arm in ARMS}

        last_arm = get_last_chosen_arm(reg_number, activity_type)
        candidates = {a: s for a, s in samples.items() if a != last_arm} if last_arm in samples else samples

        chosen_arm = max(candidates, key=candidates.get)

    set_chosen_arm(absence_id, context_key, chosen_arm, is_control)
    return chosen_arm


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

    Control-group absences (see module docstring) still get their
    reward computed and recorded -- their outcomes are exactly what
    the evaluation needs to compare against -- but deliberately never
    update_posterior, since a forced "reminder" pick was never really
    an action the adaptive policy chose, and folding it in would
    contaminate what the policy is actually learning.
    """
    cutoff = date.today() - timedelta(days=_REWARD_DELAY_DAYS)
    for absence in get_absences_awaiting_reward(cutoff):
        next_date = absence["activity_date"] + timedelta(days=_REWARD_DELAY_DAYS)
        reward = not absence_exists_for_date(absence["reg_number"], absence["activity_type"], next_date)
        set_reward(absence["id"], reward)
        if not absence["is_control"]:
            update_posterior(absence["context_key"], absence["chosen_arm"], reward)

    _compute_pending_recovery()


def _compute_pending_recovery():
    """
    Follow-up pass, same daily task -- once an absence's SECOND weekly
    occurrence has passed, computes recovered_within_2 (the proposal's
    own named evaluation metric). If the immediate reward was already 1
    (attended the very next time), recovery is trivially true too, no
    extra query needed; otherwise checks whether they attended the one
    after that instead. Purely a reporting signal -- never touches any
    posterior, computed identically for control and non-control
    absences since both groups' recovery rates are exactly what's being
    compared.
    """
    cutoff = date.today() - timedelta(days=_RECOVERY_DELAY_DAYS)
    for absence in get_absences_awaiting_recovery(cutoff):
        if absence["reward"]:
            recovered = True
        else:
            second_date = absence["activity_date"] + timedelta(days=_RECOVERY_DELAY_DAYS)
            recovered = not absence_exists_for_date(absence["reg_number"], absence["activity_type"], second_date)
        set_recovered_within_2(absence["id"], recovered)
