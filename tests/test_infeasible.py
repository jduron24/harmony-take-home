# Infeasibility tests. Verifies that the API returns HTTP 422 with a structured
# error body when the scheduling problem has no feasible solution, and that the
# response contains at least one concrete, human-readable reason.

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Infeasible input fixtures
# ---------------------------------------------------------------------------

# Case 1: a product requires capability "weld" but no resource has it.
# The pre-solve eligibility check catches this before the solver runs.
NO_ELIGIBLE_RESOURCE_INPUT = {
    "horizon": {
        "start": "2025-11-03T08:00:00",
        "end": "2025-11-03T16:00:00",
    },
    "resources": [
        {
            "id": "Fill-1",
            "capabilities": ["fill"],
            "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]],
        }
    ],
    "changeover_matrix_minutes": {"values": {"standard->standard": 0}},
    "products": [
        {
            "id": "P-999",
            "family": "standard",
            "due": "2025-11-03T12:00:00",
            "route": [
                # "weld" capability — no resource can handle this
                {"capability": "weld", "duration_minutes": 30},
            ],
        }
    ],
    "settings": {"time_limit_seconds": 10, "objective_mode": "min_tardiness"},
}

# Case 2: the operation duration (120 min) exceeds every window on every
# eligible resource (max window = 30 min). Non-preemptive scheduling cannot
# split the operation, so it can never be placed.
# Pre-solve check catches this — the solver never runs.
WINDOW_TOO_SMALL_INPUT = {
    "horizon": {
        "start": "2025-11-03T08:00:00",
        "end": "2025-11-03T16:00:00",
    },
    "resources": [
        {
            "id": "Fill-1",
            "capabilities": ["fill"],
            # Two windows, each only 30 minutes — neither fits a 120-min op
            "calendar": [
                ["2025-11-03T08:00:00", "2025-11-03T08:30:00"],
                ["2025-11-03T09:00:00", "2025-11-03T09:30:00"],
            ],
        }
    ],
    "changeover_matrix_minutes": {"values": {"standard->standard": 0}},
    "products": [
        {
            "id": "P-998",
            "family": "standard",
            "due": "2025-11-03T16:00:00",
            "route": [
                # 120 minutes — larger than any single window (30 min each)
                {"capability": "fill", "duration_minutes": 120},
            ],
        }
    ],
    "settings": {"time_limit_seconds": 10, "objective_mode": "min_tardiness"},
}

# Case 3: two products each needing 40 min on the same resource that has only
# a 60-minute window. Each operation individually fits (40 < 60), so the
# pre-solve checks pass. Together they need 80 min which exceeds capacity,
# so the solver itself must prove infeasibility.
SOLVER_LEVEL_INFEASIBLE_INPUT = {
    "horizon": {
        "start": "2025-11-03T08:00:00",
        "end":   "2025-11-03T09:00:00",   # 60-minute horizon
    },
    "resources": [
        {
            "id": "Fill-1",
            "capabilities": ["fill"],
            "calendar": [["2025-11-03T08:00:00", "2025-11-03T09:00:00"]],  # 60-min window
        }
    ],
    "changeover_matrix_minutes": {"values": {"standard->standard": 0}},
    "products": [
        {
            "id": "P-A", "family": "standard",
            "due": "2025-11-03T09:00:00",
            "route": [{"capability": "fill", "duration_minutes": 40}],
        },
        {
            "id": "P-B", "family": "standard",
            "due": "2025-11-03T09:00:00",
            "route": [{"capability": "fill", "duration_minutes": 40}],
        },
    ],
    "settings": {"time_limit_seconds": 10, "objective_mode": "min_tardiness"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_infeasible_shape(body: dict) -> None:
    """Verify the response body has the required infeasible error structure."""
    assert "error" in body, f"Response missing 'error' field: {body}"
    assert "why" in body, f"Response missing 'why' field: {body}"
    assert body["error"] == "infeasible", (
        f"Expected error='infeasible', got {body['error']!r}"
    )
    assert isinstance(body["why"], list), (
        f"Expected 'why' to be a list, got {type(body['why']).__name__}"
    )
    assert len(body["why"]) >= 1, (
        f"Expected at least one reason in 'why', got empty list"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_eligible_resource_returns_422():
    """
    A product requiring a capability that no resource has must produce HTTP 422
    with a reason that names the missing capability.
    """
    response = client.post("/schedule", json=NO_ELIGIBLE_RESOURCE_INPUT)

    assert response.status_code == 422, (
        f"Expected 422 for no-eligible-resource input, got {response.status_code}"
    )

    body = response.json()
    _assert_infeasible_shape(body)

    assert any("weld" in reason for reason in body["why"]), (
        f"Expected a reason mentioning capability 'weld', got: {body['why']}"
    )


def test_window_too_small_returns_422():
    """
    An operation whose duration exceeds every window on every eligible resource
    must produce HTTP 422 with a reason that mentions the window constraint.
    """
    response = client.post("/schedule", json=WINDOW_TOO_SMALL_INPUT)

    assert response.status_code == 422, (
        f"Expected 422 for window-too-small input, got {response.status_code}"
    )

    body = response.json()
    _assert_infeasible_shape(body)

    assert any("window" in reason.lower() for reason in body["why"]), (
        f"Expected a reason mentioning 'window', got: {body['why']}"
    )


def test_infeasible_response_has_no_assignments():
    """
    An infeasible response must never contain an 'assignments' key —
    that field belongs only to the success shape.
    """
    response = client.post("/schedule", json=NO_ELIGIBLE_RESOURCE_INPUT)

    assert response.status_code == 422
    body = response.json()

    assert "assignments" not in body, (
        f"Infeasible response must not contain 'assignments', got: {list(body.keys())}"
    )


def test_solver_proves_infeasibility():
    """
    Both operations individually fit in the 60-minute window (40 min each),
    so pre-solve checks pass. Together they need 80 min which exceeds the
    available 60 min — the solver must run and prove infeasibility itself.
    This tests the solver-level INFEASIBLE path, not the pre-solve fast-exit.
    """
    response = client.post("/schedule", json=SOLVER_LEVEL_INFEASIBLE_INPUT)

    assert response.status_code == 422, (
        f"Expected 422 for solver-level infeasible input, got {response.status_code}"
    )

    body = response.json()
    _assert_infeasible_shape(body)

    assert "assignments" not in body, (
        f"Infeasible response must not contain 'assignments', got: {list(body.keys())}"
    )
