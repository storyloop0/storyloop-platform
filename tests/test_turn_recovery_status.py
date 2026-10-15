"""The status wire type admits only the published four-field states."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from storyloop_platform.api.contracts import contract_schemas, turn_recovery_status_adapter


VALID = [
    ("complete", True, "RECEIPT_READY"),
    ("recoverable", True, "PREPARED_READY"),
    ("recoverable", True, "SCENE_READY"),
    *(("blocked", False, reason) for reason in (
        "INPUT_MISMATCH", "LATER_REQUEST", "EVIDENCE_INVALID", "CONFIG_CHANGED",
        "USAGE_UNAVAILABLE", "LEGACY_REQUEST")),
    ("not_available", False, "NO_SNAPSHOT"),
    ("not_available", False, "UNSUPPORTED"),
]


@pytest.mark.parametrize("status,can_recover,reason_code", VALID)
def test_status_contract_accepts_exact_published_combinations(status, can_recover, reason_code):
    payload = dict(request_id="request_1", status=status, can_recover=can_recover,
                   reason_code=reason_code)
    parsed = turn_recovery_status_adapter.validate_python(payload)
    assert turn_recovery_status_adapter.dump_python(parsed, mode="json") == payload
    assert "TurnRecoveryStatus" in contract_schemas()


@pytest.mark.parametrize("status,can_recover,reason_code", [
    ("complete", False, "RECEIPT_READY"),
    ("complete", True, "SCENE_READY"),
    ("recoverable", True, "NO_SNAPSHOT"),
    ("blocked", False, "UNSUPPORTED"),
    ("not_available", True, "NO_SNAPSHOT"),
    ("future", False, "NO_SNAPSHOT"),
])
def test_status_contract_rejects_inconsistent_combinations(status, can_recover, reason_code):
    with pytest.raises(ValidationError):
        turn_recovery_status_adapter.validate_python(dict(request_id="r", status=status,
            can_recover=can_recover, reason_code=reason_code))


def test_every_other_status_boolean_reason_combination_is_rejected():
    allowed = set(VALID)
    reasons = {reason for _, _, reason in VALID}
    for status in ("complete", "recoverable", "blocked", "not_available"):
        for can_recover in (False, True):
            for reason_code in reasons:
                if (status, can_recover, reason_code) in allowed:
                    continue
                with pytest.raises(ValidationError):
                    turn_recovery_status_adapter.validate_python(dict(
                        request_id="r", status=status, can_recover=can_recover,
                        reason_code=reason_code))


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(extra="unexpected"),
    lambda value: value.pop("request_id"),
    lambda value: value.update(can_recover=1),
    lambda value: value.update(request_id=123),
    lambda value: value.update(reason_code="UNKNOWN"),
])
def test_status_contract_rejects_extra_missing_and_wrong_types(mutation):
    payload = dict(request_id="r", status="complete", can_recover=True,
                   reason_code="RECEIPT_READY")
    mutation(payload)
    with pytest.raises(ValidationError):
        turn_recovery_status_adapter.validate_python(deepcopy(payload))
