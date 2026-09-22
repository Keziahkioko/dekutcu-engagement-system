"""
tests/test_allocation.py

Manual test for the Stage 5 allocation engine -- both the greedy
heuristic and the ILP solve. Loads tests/sample_members.csv, runs
each version, and prints a readable summary plus a worst-gap
comparison so we can see whether the ILP actually improves on the
greedy heuristic's weak spots -- same style as
generate_sample_members.py's print_area_summary().
"""

import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.allocation import allocate_members_greedy, allocate_members_ilp


def load_members(filepath="sample_members.csv"):
    path = os.path.join(os.path.dirname(__file__), filepath)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        members = []
        for row in reader:
            row["year_of_study"] = int(row["year_of_study"])
            members.append(row)
        return members


def print_area_report(area, area_members, groups):
    total = len(area_members)
    print(f"\n{area} -- {total} members, {len(groups)} groups")

    gender_totals = {}
    year_totals = {}
    for m in area_members:
        gender_totals[m["gender"]] = gender_totals.get(m["gender"], 0) + 1
        year_totals[m["year_of_study"]] = year_totals.get(m["year_of_study"], 0) + 1

    print(f"  Area-wide gender mix: {gender_totals}")
    print(f"  Area-wide year mix:   {year_totals}")

    for i, group in enumerate(groups, start=1):
        g_gender = {}
        g_year = {}
        for m in group:
            g_gender[m["gender"]] = g_gender.get(m["gender"], 0) + 1
            g_year[m["year_of_study"]] = g_year.get(m["year_of_study"], 0) + 1
        print(f"    Group {i} ({len(group)}): gender={g_gender}  year={g_year}")


def worst_gap(area_members, groups):
    """
    Same metric the ILP minimizes: the single worst gap, in people,
    between any group's actual gender or year-of-study count and its
    ideal proportional share. Used here to compare greedy vs ILP on
    equal terms.
    """
    n = len(area_members)

    def totals(key):
        counts = {}
        for m in area_members:
            counts[m[key]] = counts.get(m[key], 0) + 1
        return counts

    worst = 0.0
    for key in ("gender", "year_of_study"):
        for value, area_count in totals(key).items():
            share = area_count / n
            for group in groups:
                actual = sum(1 for m in group if m[key] == value)
                ideal = share * len(group)
                worst = max(worst, abs(actual - ideal))
    return worst


def check_sanity(members, result, label):
    seen_ids = []
    for groups in result["groups"].values():
        for group in groups:
            seen_ids.extend(m["member_id"] for m in group)
    for area_members in result["flagged_areas"].values():
        seen_ids.extend(m["member_id"] for m in area_members)

    all_ids = [m["member_id"] for m in members]
    status = "PASS" if sorted(seen_ids) == sorted(all_ids) else "FAIL"
    print(f"\n[{label}] Sanity check: {len(seen_ids)} members placed, "
          f"{len(set(seen_ids))} unique, {len(all_ids)} total input -- {status}")


if __name__ == "__main__":
    members = load_members()

    by_area = {}
    for m in members:
        by_area.setdefault(m["area"], []).append(m)

    greedy_result = allocate_members_greedy(members)
    ilp_result = allocate_members_ilp(members)

    print("=" * 70)
    print("GREEDY HEURISTIC")
    print("=" * 70)
    for area, groups in sorted(greedy_result["groups"].items()):
        print_area_report(area, by_area[area], groups)
    check_sanity(members, greedy_result, "greedy")

    print("\n" + "=" * 70)
    print("ILP SOLVE")
    print("=" * 70)
    for area, groups in sorted(ilp_result["groups"].items()):
        print_area_report(area, by_area[area], groups)
    check_sanity(members, ilp_result, "ilp")

    print("\n" + "=" * 70)
    print("WORST-GAP COMPARISON (lower is better; this is what the ILP minimizes)")
    print("=" * 70)
    for area in sorted(greedy_result["groups"]):
        g_worst = worst_gap(by_area[area], greedy_result["groups"][area])
        i_worst = worst_gap(by_area[area], ilp_result["groups"][area])
        print(f"  {area:20s} greedy={g_worst:.2f}  ilp={i_worst:.2f}")

    if greedy_result["flagged_areas"]:
        print("\nFlagged (too few members to form a group):")
        for area, area_members in greedy_result["flagged_areas"].items():
            print(f"  {area}: {len(area_members)} members -- {[m['name'] for m in area_members]}")
