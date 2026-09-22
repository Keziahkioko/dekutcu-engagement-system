"""
tests/test_allocation_topup.py

Manual test for allocate_members_topup. Simulates a realistic "second
run" scenario on top of tests/sample_members.csv:

  1. Run the ILP solve once to get a baseline set of group labels,
     as if allocation had already run for this membership.
  2. Add a handful of brand-new (unplaced) members to a few areas,
     covering the three cases the top-up logic needs to get right:
       - Bomas: existing groups all have room -> newcomers should
         slot into existing groups, no new group formed.
       - King'ong'o: its one existing group is already full (12) ->
         newcomers can't fit anywhere -> too few leftovers (2) to
         form a new group -> should get flagged.
       - Kahawa: was flagged in the baseline (only 4 members, all
         still group_label=None) -> adding 5 new members brings it to
         9, crossing the minimum -> should form its first-ever group.
  3. Run allocate_members_topup and check: nobody who was already
     placed moved, every newcomer that could be placed was, and the
     three scenarios above behaved as expected.
"""

import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.allocation import allocate_members_ilp, allocate_members_topup


def load_members(filepath="sample_members.csv"):
    path = os.path.join(os.path.dirname(__file__), filepath)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        members = []
        for row in reader:
            members.append({
                "reg_number": row["member_id"],   # standing in for reg_number in this test
                "name": row["name"],
                "gender": row["gender"],
                "year_of_study": int(row["year_of_study"]),
                "area": row["area"],
                "group_label": None,
            })
        return members


def make_new_member(reg_number, name, gender, year_of_study, area):
    return {
        "reg_number": reg_number,
        "name": name,
        "gender": gender,
        "year_of_study": year_of_study,
        "area": area,
        "group_label": None,
    }


if __name__ == "__main__":
    members = load_members()

    # Step 1: baseline run, as if allocation already happened once.
    baseline = allocate_members_ilp(members)
    baseline_labels = {}
    for area, groups in baseline["groups"].items():
        for i, group_members in enumerate(groups, start=1):
            label = f"{area} #{i}"
            for m in group_members:
                baseline_labels[m["reg_number"]] = label

    for m in members:
        m["group_label"] = baseline_labels.get(m["reg_number"])  # None for flagged areas

    print(f"Baseline: {len(baseline_labels)} members placed into "
          f"{len(set(baseline_labels.values()))} groups; "
          f"{sum(len(v) for v in baseline['flagged_areas'].values())} still flagged "
          f"({list(baseline['flagged_areas'].keys())}).")

    kingongo_size_before = sum(1 for m in members if m["group_label"] == "King'ong'o #1")
    print(f"King'ong'o #1 size before topup: {kingongo_size_before}")

    # Step 2: simulate new members joining.
    newcomers = [
        make_new_member("NEW1", "Test Bomas1", "F", 1, "Bomas"),
        make_new_member("NEW2", "Test Bomas2", "M", 3, "Bomas"),
        make_new_member("NEW3", "Test Bomas3", "F", 4, "Bomas"),
        make_new_member("NEW4", "Test Kingongo1", "M", 2, "King'ong'o"),
        make_new_member("NEW5", "Test Kingongo2", "F", 1, "King'ong'o"),
        make_new_member("NEW6", "Test Kahawa1", "M", 1, "Kahawa"),
        make_new_member("NEW7", "Test Kahawa2", "F", 2, "Kahawa"),
        make_new_member("NEW8", "Test Kahawa3", "M", 3, "Kahawa"),
        make_new_member("NEW9", "Test Kahawa4", "F", 1, "Kahawa"),
        make_new_member("NEW10", "Test Kahawa5", "M", 4, "Kahawa"),
    ]
    members_with_newcomers = members + newcomers

    # Step 3: run top-up.
    result = allocate_members_topup(members_with_newcomers)
    updates = result["updates"]
    flagged = result["flagged_areas"]

    print(f"\nTop-up produced {len(updates)} updates.")
    print(f"Top-up flagged areas: {[(a, len(m)) for a, m in flagged.items()]}")

    # Check 1: nobody who was already placed got moved or re-included.
    already_placed_reg_numbers = set(baseline_labels.keys())
    moved = [reg for reg in already_placed_reg_numbers if reg in updates]
    print(f"\n[Check] Already-placed members present in updates (should be 0): {len(moved)} "
          f"-- {'PASS' if not moved else 'FAIL: ' + str(moved)}")

    # Check 2: Bomas newcomers slotted into existing groups (no "Bomas #5").
    bomas_new = {reg: updates.get(reg) for reg in ("NEW1", "NEW2", "NEW3")}
    bomas_ok = all(label and label.startswith("Bomas #") and label != "Bomas #5" for label in bomas_new.values())
    print(f"[Check] Bomas newcomers placed into existing groups: {bomas_new} "
          f"-- {'PASS' if bomas_ok else 'FAIL'}")

    # Check 3: King'ong'o's existing group didn't grow past 12, and the
    # 2 leftover newcomers got flagged (too few for a new group).
    kingongo_placed = [reg for reg in ("NEW4", "NEW5") if reg in updates]
    kingongo_flagged = [m["reg_number"] for m in flagged.get("King'ong'o", [])]
    print(f"[Check] King'ong'o newcomers placed (should be 0, group was full): {kingongo_placed} "
          f"-- {'PASS' if not kingongo_placed else 'FAIL'}")
    print(f"[Check] King'ong'o newcomers flagged instead (should be both NEW4 and NEW5): "
          f"{sorted(kingongo_flagged)} -- "
          f"{'PASS' if sorted(kingongo_flagged) == ['NEW4', 'NEW5'] else 'FAIL'}")

    # Check 4: Kahawa crossed the threshold and formed its first group,
    # including its 4 ORIGINAL (previously-flagged) members too.
    kahawa_labels = {reg: updates.get(reg) for reg in ("NEW6", "NEW7", "NEW8", "NEW9", "NEW10")}
    original_kahawa = [m["reg_number"] for m in members if m["area"] == "Kahawa"]
    kahawa_original_labels = {reg: updates.get(reg) for reg in original_kahawa}
    all_kahawa_same_group = len(set(list(kahawa_labels.values()) + list(kahawa_original_labels.values()))) == 1
    print(f"[Check] Kahawa newcomers placed: {kahawa_labels}")
    print(f"[Check] Kahawa ORIGINAL (previously-flagged) members also now placed: {kahawa_original_labels}")
    print(f"[Check] All 9 Kahawa members landed in the same new group: "
          f"{'PASS' if all_kahawa_same_group else 'FAIL'}")

    # Check 5: full sanity check -- every member (original + new)
    # accounted for exactly once across (already-placed + updates + flagged).
    all_reg_numbers = [m["reg_number"] for m in members_with_newcomers]
    accounted = set(already_placed_reg_numbers) | set(updates.keys())
    for area_members in flagged.values():
        accounted |= {m["reg_number"] for m in area_members}
    missing = set(all_reg_numbers) - accounted
    print(f"\n[Check] Sanity: {len(all_reg_numbers)} total, {len(accounted)} accounted for "
          f"-- {'PASS' if not missing else 'FAIL: missing ' + str(missing)}")
