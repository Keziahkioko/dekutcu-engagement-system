"""
generate_sample_members.py

Creates a realistic-looking batch of fake DeKUTCU members for testing
the allocation engine, BEFORE we have real registration data.

Why this exists:
We can't test whether the greedy heuristic or the ILP solver actually
balance groups well unless we feed them real-looking data first --
including some awkward area totals that will genuinely test the
remainder-handling logic (range 6-10, prefer closest to 10).
"""

import csv
import random

# The ten fixed, mutually distant areas -- settled design, no GPS involved.
AREAS = [
    "Bomas", "Internal Hostels", "Nyeri View", "Catholic Hostels",
    "Nyaribo", "Embassy", "King'ong'o", "Nyeri Town", "Gate A", "Kahawa"
]

# Deliberately UNEVEN weights -- real membership is never evenly spread.
# Internal Hostels and Bomas are the most populated (common on-campus
# and near-campus choices), while somewhere like Embassy or Kahawa
# might realistically have very few members -- this tests our
# "genuinely tiny area gets flagged to a leader" rule too.
AREA_WEIGHTS = {
    "Bomas": 30,
    "Internal Hostels": 25,
    "Nyeri View": 15,
    "Catholic Hostels": 12,
    "Nyaribo": 10,
    "Embassy": 4,          # deliberately tiny -- tests the edge case
    "King'ong'o": 8,
    "Nyeri Town": 9,
    "Gate A": 14,
    "Kahawa": 7,
}

FIRST_NAMES_M = ["Brian", "Kevin", "David", "James", "Peter", "John", "Dennis", "Collins", "Victor", "Samuel"]
FIRST_NAMES_F = ["Faith", "Mercy", "Grace", "Joy", "Esther", "Purity", "Ann", "Winnie", "Sharon", "Diana"]
LAST_NAMES = ["Mwangi", "Otieno", "Kamau", "Wanjiru", "Kiptoo", "Njoroge", "Achieng", "Muriuki", "Wafula", "Chebet"]

YEARS_OF_STUDY = [1, 2, 3, 4]


def generate_members(total_members=150, seed=42):
    """
    Generates `total_members` fake member records.
    seed is fixed so the data is the same every time we run this --
    important for testing, so results are comparable run to run.
    """
    random.seed(seed)

    # Build a weighted list of areas to pick from, matching our
    # deliberately uneven distribution above.
    area_pool = []
    for area, weight in AREA_WEIGHTS.items():
        area_pool.extend([area] * weight)

    members = []
    for i in range(1, total_members + 1):
        gender = random.choice(["M", "F"])
        first_name = random.choice(FIRST_NAMES_M if gender == "M" else FIRST_NAMES_F)
        last_name = random.choice(LAST_NAMES)
        member = {
            "member_id": i,
            "name": f"{first_name} {last_name}",
            "gender": gender,
            "year_of_study": random.choice(YEARS_OF_STUDY),
            "area": random.choice(area_pool),
        }
        members.append(member)

    return members


def save_to_csv(members, filepath="sample_members.csv"):
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["member_id", "name", "gender", "year_of_study", "area"])
        writer.writeheader()
        writer.writerows(members)
    print(f"Wrote {len(members)} sample members to {filepath}")


def print_area_summary(members):
    """Quick sanity check: how many members landed in each area."""
    counts = {}
    for m in members:
        counts[m["area"]] = counts.get(m["area"], 0) + 1

    print("\nMembers per area:")
    for area in AREAS:
        print(f"  {area:20s} {counts.get(area, 0)}")


if __name__ == "__main__":
    members = generate_members(total_members=150)
    save_to_csv(members)
    print_area_summary(members)
