# Schedule validity tests. Verifies correctness properties that go beyond
# no-overlap and precedence: changeover gaps, calendar window containment,
# and determinism (identical input produces identical output).

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
        "end":   "2025-11-03T16:00:00",
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
            "standard->premium":  20,
            "premium->standard":  20,
            "premium->premium":   0,
        }
    },
    "products": [
        {
            "id": "P-100", "family": "standard",
            "due": "2025-11-03T12:30:00",
            "route": [
                {"capability": "fill",  "duration_minutes": 30},
                {"capability": "label", "duration_minutes": 20},
                {"capability": "pack",  "duration_minutes": 15},
            ],
        },
        {
            "id": "P-101", "family": "premium",
            "due": "2025-11-03T15:00:00",
            "route": [
                {"capability": "fill",  "duration_minutes": 35},
                {"capability": "label", "duration_minutes": 25},
                {"capability": "pack",  "duration_minutes": 15},
            ],
        },
        {
            "id": "P-102", "family": "standard",
            "due": "2025-11-03T13:30:00",
            "route": [
                {"capability": "fill",  "duration_minutes": 25},
                {"capability": "label", "duration_minutes": 20},
            ],
        },
        {
            "id": "P-103", "family": "premium",
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

RESOURCE_CALENDARS = {r["id"]: r["calendar"] for r in SAMPLE_INPUT["resources"]}
PRODUCT_FAMILIES   = {p["id"]: p["family"]   for p in SAMPLE_INPUT["products"]}
CHANGEOVER_MATRIX  = SAMPLE_INPUT["changeover_matrix_minutes"]["values"]


@pytest.fixture(scope="module")
def schedule_response():
    response = client.post("/schedule", json=SAMPLE_INPUT)
    assert response.status_code == 200, f"Schedule failed: {response.json()}"
    return response.json()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_minutes(iso: str) -> int:
    return int((datetime.fromisoformat(iso) - HORIZON_START).total_seconds() / 60)


def _group_by_resource(assignments: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for a in assignments:
        grouped.setdefault(a["resource"], []).append(a)
    return grouped


def _fits_in_a_window(resource_id: str, start_iso: str, end_iso: str) -> bool:
    return any(
        w[0] <= start_iso and end_iso <= w[1]
        for w in RESOURCE_CALENDARS[resource_id]
    )


# ---------------------------------------------------------------------------
# Test 1: changeover gaps match required minutes
# ---------------------------------------------------------------------------

def test_changeover_gaps_match_required(schedule_response):
    """
    For every consecutive pair of operations on the same resource where families
    differ, the actual gap (next.start - prev.end) must be >= the required
    changeover minutes from the matrix.
    """
    by_resource = _group_by_resource(schedule_response["assignments"])

    for resource_id, ops in by_resource.items():
        ops.sort(key=operator.itemgetter("start"))
        for i in range(len(ops) - 1):
            prev, curr  = ops[i], ops[i + 1]
            fam_prev    = PRODUCT_FAMILIES[prev["product"]]
            fam_curr    = PRODUCT_FAMILIES[curr["product"]]
            required    = CHANGEOVER_MATRIX.get(f"{fam_prev}->{fam_curr}", 0)
            if required == 0:
                continue
            gap = _to_minutes(curr["start"]) - _to_minutes(prev["end"])
            assert gap >= required, (
                f"Changeover gap violated on {resource_id}: "
                f"{prev['product']}({fam_prev}) -> {curr['product']}({fam_curr}) "
                f"actual gap={gap} min, required={required} min"
            )


# ---------------------------------------------------------------------------
# Test 2: every assignment fits within a calendar window
# ---------------------------------------------------------------------------

def test_assignments_fit_within_calendar_windows(schedule_response):
    """
    Every assignment must be fully contained within at least one calendar window
    of its assigned resource. An assignment that spans a break violates the
    non-preemptive constraint.
    """
    for a in schedule_response["assignments"]:
        assert _fits_in_a_window(a["resource"], a["start"], a["end"]), (
            f"{a['product']} step {a['step_index']} on {a['resource']} "
            f"({a['start']} to {a['end']}) does not fit any calendar window. "
            f"Windows: {RESOURCE_CALENDARS[a['resource']]}"
        )


# ---------------------------------------------------------------------------
# Test 3: determinism — same input produces identical output
# ---------------------------------------------------------------------------

def test_determinism():
    """
    Two consecutive calls with identical input must return bit-for-bit identical
    assignments and KPIs. Requires num_search_workers=1 in the solver (set in
    engine.py) to eliminate parallel-thread race conditions.
    """
    r1 = client.post("/schedule", json=SAMPLE_INPUT).json()
    r2 = client.post("/schedule", json=SAMPLE_INPUT).json()

    assert r1["assignments"] == r2["assignments"], (
        "Determinism violated: assignments differ between two identical calls.\n"
        f"Call 1: {r1['assignments']}\n"
        f"Call 2: {r2['assignments']}"
    )
    assert r1["kpis"] == r2["kpis"], (
        "Determinism violated: KPIs differ between two identical calls.\n"
        f"Call 1: {r1['kpis']}\n"
        f"Call 2: {r2['kpis']}"
    )
