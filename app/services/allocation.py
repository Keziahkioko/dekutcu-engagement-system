"""
app/services/allocation.py

Stage 5: The allocation engine. Three functions live here, sharing the
same group-count/sizing logic and (mostly) the same input/output
contract:

  - allocate_members_greedy: a fast round-robin heuristic.
  - allocate_members_ilp: an exact solve using integer programming.
  - allocate_members_topup: places only NEW members into groups,
    without disturbing anyone already placed (see below).

All three split each area's eligible members into Bible study groups
of 8-12 people, with each group mirroring that area's actual gender
and year-of-study mix as closely as possible -- not a forced 50/50
split, since the real membership isn't 50/50 either.

This module is pure: it takes a list of member records and returns
groups. It doesn't touch the database, WhatsApp, or know anything
about consent/masters-staff filtering -- that's the caller's job.
(Masters students and staff aren't in scope yet anyway: the system
currently only registers year_of_study 1-6, and masters/staff are
being handled manually outside the bot for now.)

Design decisions (settled during Stage 5 planning):
  - Group size range is 8-12.
  - Group count per area: among every group count k, we pick whichever
    k makes the WORST individual group's deviation from [8, 12] as
    small as possible (0 if a perfect fit exists), tie-broken toward
    more, more-evenly-sized groups. Sizes within that k are split as
    evenly as possible -- they never differ by more than one member.
    Both allocate_members_greedy and allocate_members_ilp use the
    exact same sizes for a given area -- only how members are placed
    into those groups differs.
    This matters for a specific gap: with a strict "must land exactly
    in [8,12]" rule, areas of 13, 14, or 15 members have NO valid k at
    all (1 group needs <=12, 2 groups need >=16) and would get flagged
    as if they were too small to group, even though they obviously
    aren't. Allowing the smallest unavoidable deviation instead of a
    hard cutoff closes that gap: 13 becomes one group of 13 (1 over),
    14 becomes two groups of 7 (1 under each), 15 becomes 8+7 (1
    under on one group) -- whichever has the smallest worst deviation.
    This only ever changes behavior in that narrow boundary; every
    area that already had a perfect fit is completely unaffected.
  - An area with too few members for even one group (fewer than
    MIN_GROUP_SIZE) isn't force-grouped -- it's flagged for a leader
    to review manually instead. This is the only case that still gets
    flagged -- the deviation there is large, not a 1-person nudge.

  Greedy heuristic (allocate_members_greedy):
    Members are stratified by (gender, year_of_study), and a single
    running counter deals members to groups round-robin (member i ->
    group i % k) across the whole area, processing the largest strata
    first. Because the counter never resets between strata, the final
    group sizes always land exactly on the target sizes, and each
    stratum ends up spread across groups as evenly as round-robin
    allows (off by at most one per group). This is fast but a genuine
    heuristic -- it can still leave one group noticeably skewed on
    gender or year purely by where the counter happened to be sitting
    when an odd-sized stratum was dealt (see the Bomas example from
    Stage 5 planning: one group ended up 6 male / 3 female in an area
    that was exactly 50/50 overall).

  Exact ILP solve (allocate_members_ilp):
    Solved one area at a time with PuLP (bundled CBC solver). For a
    given area with n members split into groups of the predetermined
    sizes:
      - x[m][g] is a yes/no decision: is member m placed in group g?
      - Every member is assigned to exactly one group.
      - Every group's size is pinned to its predetermined target.
      - Gender and year-of-study are checked SEPARATELY, not as
        joint (gender, year) combinations -- with only 8-12 people per
        group, joint cells are too sparse to usefully optimize, and
        the original objective was stated as two separate proportions
        (gender mix, year mix), not a joint one.
      - For every group, and separately for every gender value and
        every year-of-study value present in the area, the assignment
        implies an actual headcount in that group. That count's
        "ideal" is the area-wide proportion for that value, scaled to
        the group's size (e.g. if the area is 50% female and a group
        has 9 people, that group's ideal female count is 4.5).
      - A single variable, worst_gap, is constrained to be >= every
        one of those |actual - ideal| gaps, across every group and
        every gender/year value. The objective is simply to minimize
        worst_gap -- i.e. find the assignment where the single worst
        gap, anywhere, is as small as possible. This directly targets
        avoiding another Bomas-style skewed group, rather than just
        minimizing the average gap.
      - Capped at ILP_TIME_LIMIT_SECONDS per area. Finding a very good
        (often exactly optimal) answer is typically fast; what can run
        unboundedly long is the solver PROVING no better answer exists,
        by ruling out symmetric duplicates (it doesn't know group
        labels are arbitrary, so "A in group 1, B in group 2" and the
        relabeled-but-identical "A in group 2, B in group 1" both have
        to be separately ruled out). That search blew up in testing at
        200 members, where one area needed 5 groups instead of 4. The
        time cap means: take the best answer found within the budget,
        which in practice is excellent, even if it isn't certified as
        provably optimal.

  Top-up placement (allocate_members_topup):
    Allocation gets rerun periodically as new members trickle in --
    re-solving every area from scratch each time would reshuffle
    members who are already settled into a group, for no reason other
    than a few newcomers joining their area. This function never moves
    an already-placed member (one whose group_label is already set).
    Only members with group_label = None are placed:
      - Existing groups are reconstructed from already-placed members'
        group_label values.
      - New members are placed one at a time, into whichever existing
        group (under MAX_GROUP_SIZE) currently has the fewest people
        of their gender -- tied by fewest of their year-of-study, tied
        by smallest current size. Each placement updates that group's
        counts before the next new member is considered.
      - Anyone who doesn't fit anywhere (every existing group is full,
        or the area has no existing groups yet) becomes part of a
        leftover pool. That pool is grouped fresh -- same sizing rule,
        solved with the ILP -- into brand-new groups, numbered
        continuing on from the area's existing group numbers (or
        starting at #1 if there are none). Too few leftovers to form
        even one group (fewer than MIN_GROUP_SIZE) get flagged for
        manual placement, same as the other two functions.
    This also transparently handles the very first run: with no
    existing groups anywhere, every member in every area is "new" and
    lands in the leftover-pool path, which forms everyone's first
    groups labeled Area #1, Area #2, etc.
"""

from collections import defaultdict

import pulp

MIN_GROUP_SIZE = 8
MAX_GROUP_SIZE = 12

# Caps how long the solver spends per area. Branch-and-bound solvers
# like CBC typically find a very good (often exactly optimal) answer
# quickly -- what can run unboundedly long is PROVING no better answer
# exists, by ruling out symmetric duplicates (e.g. "person A in group 1
# and person B in group 2" vs the relabeled-but-identical "A in group 2,
# B in group 1" -- the solver doesn't know group labels are arbitrary).
# That proof search is what blew up at 200 members (a 45-person, 5-group
# area went from 12s at 150 members to several minutes uncapped). Capping
# the time means: take the best answer found within the budget, even if
# it isn't certified as provably optimal.
ILP_TIME_LIMIT_SECONDS = 15


def allocate_members_greedy(members):
    """
    Splits `members` (a list of dicts, each with at least 'area',
    'gender', and 'year_of_study' keys) into balanced groups per area,
    using the round-robin-by-stratum greedy heuristic (see module
    docstring).

    Returns a dict:
      {
        "groups": {area: [[member, ...], [member, ...], ...], ...},
        "flagged_areas": {area: [member, ...], ...},
      }
    Areas with too few members to form a valid group land in
    "flagged_areas" instead of "groups".
    """
    by_area = defaultdict(list)
    for member in members:
        by_area[member["area"]].append(member)

    groups = {}
    flagged_areas = {}

    for area, area_members in by_area.items():
        sizes = _group_sizes_for_area(len(area_members))
        if sizes is None:
            flagged_areas[area] = area_members
        else:
            groups[area] = _distribute_into_groups(area_members, sizes)

    return {"groups": groups, "flagged_areas": flagged_areas}


def allocate_members_ilp(members):
    """
    Same input/output contract as allocate_members_greedy, but instead
    of dealing members out round-robin, solves an integer program per
    area that finds the assignment minimizing the single worst gap --
    across every group, and separately for gender and year-of-study --
    between a group's actual makeup and its ideal proportional share.
    See module docstring for the full formulation.
    """
    by_area = defaultdict(list)
    for member in members:
        by_area[member["area"]].append(member)

    groups = {}
    flagged_areas = {}

    for area, area_members in by_area.items():
        sizes = _group_sizes_for_area(len(area_members))
        if sizes is None:
            flagged_areas[area] = area_members
        else:
            groups[area] = _solve_area_ilp(area_members, sizes)

    return {"groups": groups, "flagged_areas": flagged_areas}


def allocate_members_topup(members):
    """
    Places only NEW members (group_label is None) into groups, without
    moving anyone already placed (group_label already set). See module
    docstring for the full strategy.

    Each member dict must include 'reg_number' (used to key the
    result) and 'group_label' (their current label, or None), in
    addition to the 'area', 'gender', and 'year_of_study' keys the
    other two functions need.

    Returns a dict:
      {
        "updates": {reg_number: group_label, ...},
        "flagged_areas": {area: [member, ...], ...},
      }
    "updates" contains ONLY newly-placed members -- already-placed
    members aren't touched, so they're left out entirely.
    """
    by_area = defaultdict(list)
    for member in members:
        by_area[member["area"]].append(member)

    updates = {}
    flagged_areas = {}

    for area, area_members in by_area.items():
        placed = [m for m in area_members if m["group_label"] is not None]
        new_members = [m for m in area_members if m["group_label"] is None]

        if not new_members:
            continue

        existing_groups = _existing_groups_from_placed(placed)
        leftover = _fill_existing_groups(new_members, existing_groups, updates)

        if not leftover:
            continue

        sizes = _group_sizes_for_area(len(leftover))
        if sizes is None:
            flagged_areas[area] = leftover
            continue

        new_groups = _solve_area_ilp(leftover, sizes)
        next_number = _next_group_number(existing_groups.keys(), area)
        for offset, group_members in enumerate(new_groups):
            label = f"{area} #{next_number + offset}"
            for member in group_members:
                updates[member["reg_number"]] = label

    return {"updates": updates, "flagged_areas": flagged_areas}


def _existing_groups_from_placed(placed):
    """Reconstructs {group_label: [member, ...]} from already-placed members."""
    groups = defaultdict(list)
    for member in placed:
        groups[member["group_label"]].append(member)
    return groups


def _fill_existing_groups(new_members, existing_groups, updates):
    """
    Places as many new_members as possible into existing_groups
    (mutated in place as members are added, so later placements see
    up-to-date counts). Writes each placement into `updates`. Returns
    the list of new_members that didn't fit anywhere.
    """
    leftover = []
    for member in new_members:
        label = _best_existing_group(member, existing_groups)
        if label is None:
            leftover.append(member)
        else:
            existing_groups[label].append(member)
            updates[member["reg_number"]] = label
    return leftover


def _best_existing_group(member, existing_groups):
    """
    Returns the label of whichever group (under MAX_GROUP_SIZE) has
    the fewest members sharing this member's gender -- tied by fewest
    sharing their year-of-study, tied by smallest current size. None
    if every existing group is already full.
    """
    candidates = [
        label for label, group_members in existing_groups.items()
        if len(group_members) < MAX_GROUP_SIZE
    ]
    if not candidates:
        return None

    def deficit(label):
        group_members = existing_groups[label]
        same_gender = sum(1 for m in group_members if m["gender"] == member["gender"])
        same_year = sum(1 for m in group_members if m["year_of_study"] == member["year_of_study"])
        return (same_gender, same_year, len(group_members))

    return min(candidates, key=deficit)


def _next_group_number(existing_labels, area):
    """
    Parses "{area} #N" labels to find the next available N for this
    area (1 if there are no existing labels).
    """
    prefix = f"{area} #"
    numbers = [int(label[len(prefix):]) for label in existing_labels if label.startswith(prefix)]
    return max(numbers, default=0) + 1


def _solve_area_ilp(members, sizes):
    """
    Solves one area's group assignment as a mixed-integer program (see
    module docstring for the full formulation). Group labels are
    interchangeable -- which specific group ends up with which
    `sizes` entry is arbitrary, the solver just needs exactly one
    group of each size in `sizes`.
    """
    n = len(members)
    k = len(sizes)
    group_indices = range(k)

    total_by_gender = defaultdict(int)
    total_by_year = defaultdict(int)
    for member in members:
        total_by_gender[member["gender"]] += 1
        total_by_year[member["year_of_study"]] += 1

    problem = pulp.LpProblem("group_allocation", pulp.LpMinimize)

    x = {
        (i, g): pulp.LpVariable(f"x_{i}_{g}", cat="Binary")
        for i in range(n)
        for g in group_indices
    }
    worst_gap = pulp.LpVariable("worst_gap", lowBound=0)

    problem += worst_gap

    # Every member assigned to exactly one group.
    for i in range(n):
        problem += pulp.lpSum(x[i, g] for g in group_indices) == 1

    # Every group's size fixed to its predetermined target.
    for g in group_indices:
        problem += pulp.lpSum(x[i, g] for i in range(n)) == sizes[g]

    # worst_gap bounds every gender-count gap, in every group.
    for gender, area_count in total_by_gender.items():
        share = area_count / n
        member_indices = [i for i, m in enumerate(members) if m["gender"] == gender]
        for g in group_indices:
            actual = pulp.lpSum(x[i, g] for i in member_indices)
            ideal = share * sizes[g]
            problem += worst_gap >= actual - ideal
            problem += worst_gap >= ideal - actual

    # worst_gap bounds every year-of-study-count gap, in every group.
    for year, area_count in total_by_year.items():
        share = area_count / n
        member_indices = [i for i, m in enumerate(members) if m["year_of_study"] == year]
        for g in group_indices:
            actual = pulp.lpSum(x[i, g] for i in member_indices)
            ideal = share * sizes[g]
            problem += worst_gap >= actual - ideal
            problem += worst_gap >= ideal - actual

    problem.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=ILP_TIME_LIMIT_SECONDS))

    groups = [[] for _ in group_indices]
    for i, member in enumerate(members):
        for g in group_indices:
            if pulp.value(x[i, g]) > 0.5:
                groups[g].append(member)
                break

    return groups


def _group_sizes_for_area(n, min_size=MIN_GROUP_SIZE, max_size=MAX_GROUP_SIZE):
    """
    Returns the list of group sizes to use for an area with `n`
    eligible members, or None if n is too small to form even one
    group (n < min_size) -- that's the only case still flagged.

    Among every group count k, picks whichever makes the WORST
    individual group's deviation from [min_size, max_size] as small
    as possible (0 if a perfect fit exists somewhere), tie-broken
    toward larger k (more, more-evenly-sized groups). This closes the
    13/14/15-style gap where a strict "must fit exactly" rule would
    otherwise find no valid k at all -- see module docstring.
    """
    if n < min_size:
        return None

    best_k, best_deviation = None, None
    # +2 is a safety margin past the point where deviation starts
    # strictly increasing again -- the true best k is always well
    # within this range for realistic area sizes.
    for k in range(1, n // min_size + 2):
        deviation = _worst_size_deviation(n, k, min_size, max_size)
        if best_deviation is None or deviation < best_deviation or (deviation == best_deviation and k > best_k):
            best_k, best_deviation = k, deviation

    base, remainder = divmod(n, best_k)
    return [base + 1] * remainder + [base] * (best_k - remainder)


def _worst_size_deviation(n, k, min_size, max_size):
    """How far outside [min_size, max_size] the worst of k even groups falls (0 if all fit)."""
    base, remainder = divmod(n, k)
    sizes = [base + 1] * remainder + [base] * (k - remainder)
    return max(max(0, min_size - s, s - max_size) for s in sizes)


def _distribute_into_groups(members, sizes):
    """
    Deals `members` into len(sizes) groups, round-robin, processing
    the largest (gender, year_of_study) stratum first. A single
    counter runs across all strata (never resets), so the resulting
    group sizes always match `sizes` exactly.
    """
    k = len(sizes)
    groups = [[] for _ in range(k)]

    strata = defaultdict(list)
    for member in members:
        strata[(member["gender"], member["year_of_study"])].append(member)

    counter = 0
    for key in sorted(strata, key=lambda s: -len(strata[s])):
        for member in strata[key]:
            groups[counter % k].append(member)
            counter += 1

    return groups
