# KPI correctness tests. Independently recomputes each KPI from raw API output
# and verifies it matches what the API reported. Proves numbers derive from real
# assignments, not hardcoded values — an explicit acceptance check for the take-home.

import operator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

HORIZON_START = datetime.fromisoformat("2025-11-03T08:00:00")

SAMPLE_INPUT = {
    "horizon": {
        "start": "2025-11-03T08:00:00",
        "end": "2025-11-03T16:00:00",
    },
    "resources": [
        {
            "id": "Fill-1",
            "capabilities": ["fill"],
            "calendar": [
                ["2025-11-03T08:00:00", "2025-11-03T12:00:00"],
                ["2025-11-03T12:30:00", "2025-11-03T16:00:00"],
            ],
        },
        {
            "id": "Fill-2",
            "capabilities": ["fill"],
            "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]],
        },
        {
            "id": "Label-1",
            "capabilities": ["label"],
            "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]],
        },
        {
            "id": "Pack-1",
            "capabilities": ["pack"],
            "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]],
        },
    ],
    "changeover_matrix_minutes": {
        "values": {
            "standard->standard": 0,
            "standard->premium": 20,
            "premium->standard": 20,
            "premium->premium": 0,
        }
    },
    "products": [
        {
            "id": "P-100",
            "family": "standard",
            "due": "2025-11-03T12:30:00",
            "route": [
                {"capability": "fill",  "duration_minutes": 30},
                {"capability": "label", "duration_minutes": 20},
                {"capability": "pack",  "duration_minutes": 15},
            ],
        },
        {
            "id": "P-101",
            "family": "premium",
            "due": "2025-11-03T15:00:00",
            "route": [
                {"capability": "fill",  "duration_minutes": 35},
                {"capability": "label", "duration_minutes": 25},
                {"capability": "pack",  "duration_minutes": 15},
            ],
        },
        {
            "id": "P-102",
            "family": "standard",
            "due": "2025-11-03T13:30:00",
            "route": [
                {"capability": "fill",  "duration_minutes": 25},
                {"capability": "label", "duration_minutes": 20},
            ],
        },
        {
            "id": "P-103",
            "family": "premium",
            "due": "2025-11-03T14:00:00",
            "route": [
                {"capability": "fill",  "duration_minutes": 30},
                {"capability": "label", "duration_minutes": 20},
                {"capability": "pack",  "duration_minutes": 15},
            ],
        },
    ],
    "settings": {"time_limit_seconds": 30, "objective_mode": "min_tardiness"},
}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def schedule_response():
    """Call POST /schedule once and share the parsed response across all tests."""
    response = client.post("/schedule", json=SAMPLE_INPUT)
    assert response.status_code == 200, (
        f"Schedule request failed: {response.json()}"
    )
    return response.json()


# ---------------------------------------------------------------------------
# Shared datetime helper
# ---------------------------------------------------------------------------

def _to_minutes(iso_string: str) -> int:
    """Convert an ISO datetime string to integer minutes offset from HORIZON_START."""
    dt = datetime.fromisoformat(iso_string)
    return int((dt - HORIZON_START).total_seconds() / 60)


# ---------------------------------------------------------------------------
# Independent KPI recomputers
# ---------------------------------------------------------------------------

def _recompute_tardiness(assignments: list[dict], products: list[dict]) -> int:
    """
    Recompute total tardiness: sum of max(0, completion - due) per product.
    Completion is the latest end time across a product's assignments.
    Due times are parsed from the SAMPLE_INPUT products list.
    Products with no assignments are skipped (contribute 0).
    """
    due_by_product_id = {p["id"]: _to_minutes(p["due"]) for p in products}

    completion_by_product_id: dict[str, int] = {}
    for assignment in assignments:
        product_id = assignment["product"]
        end_minutes = _to_minutes(assignment["end"])
        current_max = completion_by_product_id.get(product_id, 0)
        completion_by_product_id[product_id] = max(current_max, end_minutes)

    total_tardiness = 0
    for product_id, due_minutes in due_by_product_id.items():
        if product_id not in completion_by_product_id:
            continue
        total_tardiness += max(0, completion_by_product_id[product_id] - due_minutes)

    return total_tardiness


def _recompute_makespan(assignments: list[dict]) -> int:
    """
    Recompute makespan: latest end minus earliest start across all assignments.
    Both values parsed from ISO strings via _to_minutes().
    Returns 0 for empty input.
    """
    if not assignments:
        return 0

    earliest_start = min(_to_minutes(a["start"]) for a in assignments)
    latest_end     = max(_to_minutes(a["end"])   for a in assignments)

    return latest_end - earliest_start


def _recompute_changeovers(
    assignments: list[dict],
    products: list[dict],
    changeover_matrix: dict,
) -> tuple[int, int]:
    """
    Recompute changeover count and total minutes from consecutive assignment pairs
    on the same resource. Product families come from the SAMPLE_INPUT products list.
    Missing matrix keys are treated as 0 setup time (not counted).
    Returns (count, total_minutes).
    """
    family_by_product_id = {p["id"]: p["family"] for p in products}

    by_resource: dict[str, list[dict]] = {}
    for assignment in assignments:
        by_resource.setdefault(assignment["resource"], []).append(assignment)

    changeover_count = 0
    changeover_minutes = 0

    for resource_assignments in by_resource.values():
        resource_assignments.sort(key=operator.itemgetter("start"))
        for i in range(len(resource_assignments) - 1):
            family_current = family_by_product_id[resource_assignments[i]["product"]]
            family_next    = family_by_product_id[resource_assignments[i + 1]["product"]]
            setup_minutes  = changeover_matrix.get(f"{family_current}->{family_next}", 0)
            if setup_minutes > 0:
                changeover_count   += 1
                changeover_minutes += setup_minutes

    return changeover_count, changeover_minutes


def _recompute_utilization(
    assignments: list[dict],
    resources: list[dict],
) -> dict[str, int]:
    """
    Recompute per-resource utilization percentage.
    Numerator: sum of processing minutes (end - start) per resource.
    Denominator: sum of calendar window durations from SAMPLE_INPUT.
    Changeover gaps fall between assignments and are excluded automatically.
    Returns dict of resource_id -> rounded integer percentage.
    """
    by_resource: dict[str, list[dict]] = {}
    for assignment in assignments:
        by_resource.setdefault(assignment["resource"], []).append(assignment)

    utilization: dict[str, int] = {}
    for resource in resources:
        resource_id = resource["id"]
        processing_minutes = sum(
            _to_minutes(a["end"]) - _to_minutes(a["start"])
            for a in by_resource.get(resource_id, [])
        )
        available_minutes = sum(
            _to_minutes(window[1]) - _to_minutes(window[0])
            for window in resource["calendar"]
        )
        if available_minutes == 0:
            utilization[resource_id] = 0
        else:
            utilization[resource_id] = round(processing_minutes / available_minutes * 100)

    return utilization


# ---------------------------------------------------------------------------
# Tests — one per KPI
# ---------------------------------------------------------------------------

def test_tardiness_matches(schedule_response):
    """Recomputed tardiness must be within ±1 minute of the reported value."""
    reported   = schedule_response["kpis"]["tardiness_minutes"]
    recomputed = _recompute_tardiness(
        schedule_response["assignments"],
        SAMPLE_INPUT["products"],
    )
    assert abs(recomputed - reported) <= 1, (
        f"tardiness_minutes mismatch: "
        f"recomputed {recomputed} but API reported {reported} "
        f"(tolerance ±1 minute)"
    )


def test_makespan_matches(schedule_response):
    """Recomputed makespan must exactly match the reported value."""
    reported   = schedule_response["kpis"]["makespan_minutes"]
    recomputed = _recompute_makespan(schedule_response["assignments"])
    assert recomputed == reported, (
        f"makespan_minutes mismatch: "
        f"recomputed {recomputed} but API reported {reported}"
    )


def test_changeovers_match(schedule_response):
    """Recomputed changeover count and minutes must exactly match reported values."""
    reported_count, reported_minutes = (
        schedule_response["kpis"]["changeover_count"],
        schedule_response["kpis"]["changeover_minutes"],
    )
    recomputed_count, recomputed_minutes = _recompute_changeovers(
        schedule_response["assignments"],
        SAMPLE_INPUT["products"],
        SAMPLE_INPUT["changeover_matrix_minutes"]["values"],
    )
    assert recomputed_count == reported_count, (
        f"changeover_count mismatch: "
        f"recomputed {recomputed_count} but API reported {reported_count}"
    )
    assert recomputed_minutes == reported_minutes, (
        f"changeover_minutes mismatch: "
        f"recomputed {recomputed_minutes} but API reported {reported_minutes}"
    )


def test_utilization_matches(schedule_response):
    """Per-resource utilization must be within ±1 percent of the reported value."""
    reported   = schedule_response["kpis"]["utilization_pct"]
    recomputed = _recompute_utilization(
        schedule_response["assignments"],
        SAMPLE_INPUT["resources"],
    )
    for resource_id, reported_pct in reported.items():
        recomputed_pct = recomputed.get(resource_id, 0)
        assert abs(recomputed_pct - reported_pct) <= 1, (
            f"utilization_pct mismatch for {resource_id}: "
            f"recomputed {recomputed_pct}% but API reported {reported_pct}% "
            f"(tolerance ±1%)"
        )
