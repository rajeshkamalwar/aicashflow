"""Canonical Amazon transaction status-transition policy."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any


def transaction_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def transition_decision(
    current: Any,
    observed_status: str,
    observed_at: str | None,
    retrieved_at: str,
    fingerprint: str,
) -> dict[str, object]:
    if current is None:
        return {
            "promote": True, "conflict": False, "category": None,
            "included": True, "reason": "Initial authoritative observation.",
        }
    previous_status = str(current["status"])
    previous_at = current["observation_at"]
    if not previous_at:
        try:
            previous_at = json.loads(current["payload_json"]).get("postedDate")
        except (TypeError, ValueError, json.JSONDecodeError):
            previous_at = None
    old_event = transaction_timestamp(previous_at)
    new_event = transaction_timestamp(observed_at)
    old_retrieval = transaction_timestamp(current["retrieved_at"])
    new_retrieval = transaction_timestamp(retrieved_at)
    if old_event is not None and new_event is not None:
        temporal = (new_event > old_event) - (new_event < old_event)
        evidence = "Amazon postedDate"
    elif old_retrieval is not None and new_retrieval is not None:
        temporal = (new_retrieval > old_retrieval) - (new_retrieval < old_retrieval)
        evidence = "retrieval timestamp"
    else:
        temporal = 0
        evidence = "available timestamps"

    if previous_status == observed_status:
        promote = temporal > 0 or (
            temporal == 0 and fingerprint > str(current["fingerprint"])
        )
        return {
            "promote": promote,
            "conflict": bool(current["conflict"]),
            "category": current["conflict_category"],
            "included": bool(current["included_in_totals"]) if promote else False,
            "reason": (
                f"Same-status observation selected by newer {evidence}."
                if temporal > 0 else "Same-status deterministic tie; status is unchanged."
            ),
        }
    if previous_status == "DEFERRED" and observed_status == "DEFERRED_RELEASED":
        return {
            "promote": True, "conflict": False,
            "category": "DOCUMENTED_DEFERRED_RELEASE", "included": True,
            "reason": "Amazon documents DEFERRED to DEFERRED_RELEASED as the release transition.",
        }
    if previous_status == "DEFERRED" and observed_status == "RELEASED":
        return {
            "promote": False, "conflict": True,
            "category": "UNUSUAL_DEFERRED_TO_RELEASED", "included": False,
            "reason": "DEFERRED to RELEASED is not the documented deferred-release transition.",
        }
    if observed_status == "DEFERRED" and previous_status in {"RELEASED", "DEFERRED_RELEASED"}:
        return {
            "promote": False, "conflict": True,
            "category": "STATUS_REGRESSION", "included": False,
            "reason": f"{previous_status} to DEFERRED is a regression requiring review.",
        }
    if {previous_status, observed_status} == {"RELEASED", "DEFERRED_RELEASED"}:
        if temporal > 0:
            return {
                "promote": True, "conflict": False,
                "category": "SEMANTIC_RELEASE_STATUS_CHANGE", "included": True,
                "reason": f"Newer {evidence} selected the observed release status.",
            }
        return {
            "promote": False, "conflict": True,
            "category": "AMBIGUOUS_RELEASE_STATUS", "included": False,
            "reason": (
                "RELEASED and DEFERRED_RELEASED cannot be resolved without a newer "
                "authoritative timestamp."
            ),
        }
    return {
        "promote": False, "conflict": True,
        "category": "UNKNOWN_STATUS_TRANSITION", "included": False,
        "reason": f"Unsupported status transition {previous_status} to {observed_status}.",
    }
