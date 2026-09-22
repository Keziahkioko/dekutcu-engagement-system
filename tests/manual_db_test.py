"""
tests/manual_db_test.py

One-off manual test of the Stage 5 wiring against the REAL Render/Neon
database. NOT part of the automated test suite -- run by hand, and
only while DATABASE_URL points at a database you're okay seeding with
fake data.

Inserts synthetic members (reg_number prefixed "TEST-", area names
that don't collide with any real DeKUTCU area) in two phases, mirroring
a realistic "allocate once, then top up later" sequence:

  Phase 1: seeds TestVille (12 members -> exactly fills one group) and
  TestTown (9 members -> one group with room to spare), then runs the
  real allocate_members_ilp + set_group_labels against the live DB, as
  if this were the very first allocation run.

  Phase 2: adds 2 more to TestVille (group already full -> should get
  flagged), 3 more to TestTown (room -> should get absorbed into the
  existing group), and introduces TestCity from scratch with 9 members
  (no existing group -> should form its first one). Then runs the real
  allocate_members_topup + set_group_labels.

Does NOT call send_whatsapp_message -- this only exercises the
database + allocation-engine wiring, not actual message delivery.

IMPORTANT: leaves all TEST-prefixed rows in the database when it's
done, on purpose, so results can be inspected. Delete them before
deploying -- see tests/cleanup_test_members.py.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from app.models.member import create_member, get_data_consenting_members, set_group_labels
from app.services.allocation import allocate_members_ilp, allocate_members_topup

FIRST_NAMES = ["Alex", "Brian", "Cathy", "Diana", "Evans", "Faith", "Grace", "Henry",
               "Irene", "James", "Kevin", "Linda"]


def seed(reg_number, area, gender, year_of_study):
    create_member(
        reg_number=reg_number,
        whatsapp_id=None,
        name=f"TEST {reg_number}",
        gender=gender,
        year_of_study=year_of_study,
        area=area,
        data_consent=True,
        followup_consent=True,
        registered_at=datetime.now(timezone.utc).isoformat(),
    )


def labels_from_ilp_groups(groups_by_area):
    updates = {}
    for area, groups in groups_by_area.items():
        for i, group_members in enumerate(groups, start=1):
            label = f"{area} #{i}"
            for m in group_members:
                updates[m["reg_number"]] = label
    return updates


def print_test_members(header):
    members = get_data_consenting_members()
    test_members = [m for m in members if m["reg_number"].startswith("TEST-")]
    print(f"\n{header} ({len(test_members)} TEST members):")
    by_label = {}
    for m in test_members:
        by_label.setdefault(m["group_label"], []).append(m["reg_number"])
    for label, regs in sorted(by_label.items(), key=lambda kv: (kv[0] is None, kv[0])):
        print(f"  {label!r}: {len(regs)} member(s) -- {regs}")


if __name__ == "__main__":
    print("=" * 70)
    print("PHASE 1: seed TestVille (12) and TestTown (9), run initial ILP allocation")
    print("=" * 70)

    for i in range(12):
        seed(f"TEST-TV-{i+1:02d}", "TestVille", "M" if i % 2 == 0 else "F", (i % 4) + 1)
    for i in range(9):
        seed(f"TEST-TT-{i+1:02d}", "TestTown", "M" if i % 2 == 0 else "F", (i % 4) + 1)

    all_members = get_data_consenting_members()
    result = allocate_members_ilp(all_members)
    updates = labels_from_ilp_groups(result["groups"])
    set_group_labels(updates)

    print(f"Seeded 21 members. ILP placed {len(updates)} of them "
          f"(the rest of the DB -- the 1 real member -- was flagged, expected).")
    print_test_members("After Phase 1")

    print("\n" + "=" * 70)
    print("PHASE 2: add overflow to TestVille (+2), room in TestTown (+3), "
          "new area TestCity (+9). Run top-up.")
    print("=" * 70)

    for i in range(2):
        seed(f"TEST-TV-OVERFLOW-{i+1}", "TestVille", "M", 2)
    for i in range(3):
        seed(f"TEST-TT-NEW-{i+1}", "TestTown", "F", 3)
    for i in range(9):
        seed(f"TEST-TC-{i+1:02d}", "TestCity", "M" if i % 2 == 0 else "F", (i % 4) + 1)

    all_members = get_data_consenting_members()
    result = allocate_members_topup(all_members)
    updates = result["updates"]
    set_group_labels(updates)

    print(f"Top-up placed {len(updates)} new members.")
    print(f"Top-up flagged: {[(a, len(m)) for a, m in result['flagged_areas'].items()]}")
    print_test_members("After Phase 2")

    print("\n" + "=" * 70)
    print("REMINDER: TEST-prefixed rows are now in the live database.")
    print("Run tests/cleanup_test_members.py before deploying for real use.")
    print("=" * 70)
