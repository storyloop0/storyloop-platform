"""Estimates for reported structured attempts under a frozen billing policy."""

from storyloop_harness.usage import AttemptUsageSummary
from storyloop_platform.portal.billing import BillingPolicy


def estimate_attempt_costs(summary: AttemptUsageSummary,
                           policy: BillingPolicy | None) -> dict:
    known = list(summary.known_usage)
    priced = [usage for usage in known if policy is not None and usage.model in policy.rates]
    unpriced = list(dict.fromkeys(usage.model for usage in known
                                  if policy is None or usage.model not in policy.rates))
    cost = policy.price_milli_points(priced) if priced and policy is not None else None
    return {
        "known_cost_milli_points": cost,
        "total_cost_milli_points": (cost if summary.usage_complete and not unpriced else None),
        "unpriced_models": unpriced,
    }
