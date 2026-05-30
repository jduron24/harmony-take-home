# Invariant tests: verify the scheduler never produces overlapping assignments on the
# same resource and always respects product route ordering (precedence).
# Both tests share a single API call via a module-scoped fixture.

import operator

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

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
# Fixture — shared across all tests in this module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def schedule_response():
    """Call POST /schedule once and share the result across all tests in this file."""
    response = client.post("/schedule", json=SAMPLE_INPUT)
    assert response.status_code == 200, (
        f"Schedule request failed: {response.json()}"
    )
    return response.json()


# ---------------------------------------------------------------------------
# Helpers — pure functions that take data and return results
# ---------------------------------------------------------------------------

def _group_by_resource(assignments: list[dict]) -> dict[str, list[dict]]:
    """Return assignments keyed by resource id."""
    grouped: dict[str, list[dict]] = {}
    for assignment in assignments:
        grouped.setdefault(assignment["resource"], []).append(assignment)
    return grouped


def _group_by_product(assignments: list[dict]) -> dict[str, list[dict]]:
    """Return assignments keyed by product id."""
    grouped: dict[str, list[dict]] = {}
    for assignment in assignments:
        grouped.setdefault(assignment["product"], []).append(assignment)
    return grouped


def _intervals_overlap(a: dict, b: dict) -> bool:
    """True if two assignments overlap: a.start < b.end AND b.start < a.end."""
    return a["start"] < b["end"] and b["start"] < a["end"]


# ---------------------------------------------------------------------------
# Test: no two assignments on the same resource may overlap
# ---------------------------------------------------------------------------

def test_no_overlap(schedule_response):
    """Every (a, b) pair sharing a resource must be non-overlapping."""
    assignments = schedule_response["assignments"]
    by_resource = _group_by_resource(assignments)

    for resource_assignments in by_resource.values():
        for i, a in enumerate(resource_assignments):
            for b in resource_assignments[i + 1:]:
                assert not _intervals_overlap(a, b), (
                    f"Overlap detected on {a['resource']}: "
                    f"{a['product']} step {a['step_index']} ({a['start']} to {a['end']}) "
                    f"overlaps with "
                    f"{b['product']} step {b['step_index']} ({b['start']} to {b['end']})"
                )


# ---------------------------------------------------------------------------
# Test: each product's steps must execute in route order
# ---------------------------------------------------------------------------

def test_precedence(schedule_response):
    """For every product, step N must end before step N+1 starts."""
    assignments = schedule_response["assignments"]
    by_product = _group_by_product(assignments)

    for product_id, product_assignments in by_product.items():
        sorted_steps = sorted(product_assignments, key=operator.itemgetter("step_index"))
        for i in range(len(sorted_steps) - 1):
            prev = sorted_steps[i]
            curr = sorted_steps[i + 1]
            assert curr["start"] >= prev["end"], (
                f"Precedence violated for {product_id}: "
                f"step {curr['step_index']} starts at {curr['start']} "
                f"but step {prev['step_index']} ends at {prev['end']}"
            )
