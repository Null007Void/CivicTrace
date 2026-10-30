#!/usr/bin/env python3
"""Deterministic smoke test of the local, OFFLINE case and both adversarial pairs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from civictrace.court import Court

court=Court.from_directory()
checks={
    'A_positive_authorization': ('authorization','DOCUMENTED_ACTION'),
    'A_negative_spent': ('spent','NOT_VERIFIED_FROM_SELECTED_RECORDS'),
    'B_positive_event': ('event','DOCUMENTED_ACTION'),
    'B_negative_construction': ('construction_from_event','WRONG_ACTION_SOURCE'),
    'C_reported_phase': ('status','CITY_REPORTED_STATUS'),
    'D_completion_unknown': ('completed','NOT_VERIFIED_FROM_SELECTED_RECORDS'),
}
failed=[]
for name,(claim,expected) in checks.items():
    got=court.evaluate(claim)
    print(f'{"PASS" if got.status==expected else "FAIL"} {name}: {got.status}')
    if got.status!=expected:failed.append(name)
print('IMPORTANT: this check is offline only, not a provider run; independent source audit remains pending.')
if failed:raise SystemExit(1)
