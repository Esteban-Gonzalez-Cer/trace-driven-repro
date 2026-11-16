#!/bin/zsh
# Phase 5b final run (docs/ANALYSIS_PLAN_B2.md): the two arms alternate bug by bug, so both arms
# of a bug run close together in time; the arm that goes first alternates from bug to bug.
#
#     zsh scripts/final-b2.sh LOGFILE
#
# Each step is rerun until it finishes. The harness exits with 3 at the usage guard or the
# plan's limit (wait 15 minutes, then resume) and with 1 when a trial hit an infrastructure
# failure (it was set aside; rerun it, at most 3 times in a row). Finished trials are skipped,
# so the script can be restarted at any time.
set -u
cd "$(dirname "$0")/.."
LOG=$1
ARM_B=benchmark/configs/final-b2-arm-b.yaml
ARM_B2=benchmark/configs/final-b2-arm-b2.yaml
PY=.venv/bin/python

step() {  # config bug
  local infra=0 code
  for i in $(seq 1 200); do
    echo "=== $2 $(basename $1 .yaml) attempt $i start $(date -u +%FT%TZ)" >> $LOG
    $PY -m tracerepro.experiment "$1" --bug "$2" >> $LOG 2>&1
    code=$?
    echo "=== $2 $(basename $1 .yaml) attempt $i exit $code $(date -u +%FT%TZ)" >> $LOG
    if [ $code -eq 0 ]; then return 0; fi
    if [ $code -eq 3 ]; then infra=0; sleep 900; continue; fi
    if [ $code -eq 1 ]; then infra=$((infra + 1)); [ $infra -ge 3 ] && return 1; sleep 120; continue; fi
    return $code
  done
  return 1
}

bugs=($(sed -n 's#^  - benchmark/manifests/\(.*\)\.yaml.*#\1#p' $ARM_B2))
n=0
for bug in $bugs; do
  if [ $((n % 2)) -eq 0 ]; then first=$ARM_B; second=$ARM_B2; else first=$ARM_B2; second=$ARM_B; fi
  step $first $bug || echo "=== $bug $(basename $first .yaml) left unfinished" >> $LOG
  step $second $bug || echo "=== $bug $(basename $second .yaml) left unfinished" >> $LOG
  n=$((n + 1))
done
echo "=== done $(date -u +%FT%TZ)" >> $LOG
