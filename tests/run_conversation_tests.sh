#!/usr/bin/env bash
# Runs tests/test_conversations.py split across parallel processes (each with its own QA_SHARD, so
# their test members, numbers and events never collide). Needed because every database call from a
# laptop crosses to Neon (us-east-2): one process alone takes the best part of an hour.
cd "$(dirname "$0")/.."
PY=venv/Scripts/python.exe; [ -x "$PY" ] || PY=python
TEST_SETS=(
  "BasicInteraction StopKeyword UnpromptedAbsence"
  "ConfirmationTraps OtherLeaderFlows NeverLoop"
  "RsvpFlow Security"
  "EventCreation Registration"
  "FellowshipCheckin Webhook BibleFollowUps"
  "Fuzz"
  "Announcements"
  "NumberChange"
  # One set, run in order: the current study guide and the Guides Coordinator are single,
  # database-wide positions, so these classes must never run side by side.
  "GuidesCoordinator GuideBatches GuidePaymentNotices GuideHandover GuideReports"
)
i=0; pids=()
for g in "${TEST_SETS[@]}"; do
  i=$((i+1)); args=""; for c in $g; do args="$args tests.test_conversations.$c"; done
  QA_SHARD=$i PYTHONIOENCODING=utf-8 "$PY" -m unittest $args > "tests/.qa_shard_$i.log" 2>&1 &
  pids+=($!)
done
status=0
for p in "${pids[@]}"; do wait "$p" || status=1; done
for n in $(seq 1 $i); do echo "== shard $n"; grep -E "^(FAIL|ERROR):|^Ran |^OK|^FAILED" "tests/.qa_shard_$n.log"; done
exit $status
