from storyloop_harness.usage import ModelAttempt, ModelUsage, summarize_attempt_usage
from storyloop_platform.portal.billing import BillingPolicy
from storyloop_platform.portal.attempt_costs import estimate_attempt_costs


def policy():
    return BillingPolicy.from_dict({
        "welcome_points": 0, "points_per_rmb": 100, "pricing_version": "v1",
        "models": {"m": {"input_rmb_per_million": "1", "output_rmb_per_million": "10",
                         "cached_input_rmb_per_million": "0"}},
    }, {})


def attempt(ordinal, model="m", usage=None, state="reported", outcome="succeeded", complete=True):
    return ModelAttempt("call", ordinal, model, "task", outcome, usage, state, complete,
                        None if outcome == "succeeded" else "RuntimeError")


def test_aggregate_known_usage_once_and_keep_unknown_total():
    records = [attempt(1, usage=ModelUsage("m", "task", 1000, 500, 0), outcome="failed"),
               attempt(2, usage=ModelUsage("m", "task", 1200, 600, 0)),
               attempt(3, state="missing")]
    summary = summarize_attempt_usage(records)
    assert estimate_attempt_costs(summary, policy()) == {
        "known_cost_milli_points": 1320, "total_cost_milli_points": None,
        "unpriced_models": [],
    }


def test_unpriced_zero_and_absent_policy():
    summary = summarize_attempt_usage([attempt(1, usage=ModelUsage("m", "task", 0, 0))])
    assert estimate_attempt_costs(summary, policy())["total_cost_milli_points"] == 0
    assert estimate_attempt_costs(summary, None)["known_cost_milli_points"] is None
    unknown = summarize_attempt_usage([attempt(1, model="other", usage=ModelUsage("other", "task", 1, 1))])
    assert estimate_attempt_costs(unknown, policy()) == {
        "known_cost_milli_points": None, "total_cost_milli_points": None,
        "unpriced_models": ["other"],
    }
