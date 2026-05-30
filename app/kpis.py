# Pure KPI calculations derived from the solver's assignment output.
# No OR-Tools, no FastAPI, no I/O. Takes the assignments list produced by
# engine.solve() and the InternalModel and returns the kpis dict that
# main.py will pass directly to the output schema.

from __future__ import annotations

import operator
from collections import defaultdict

from app.models.internal import InternalModel


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_kpis(assignments: list[dict], model: InternalModel) -> dict:
    """Assemble all KPIs into a single dict. Asserts catch calculation bugs."""
    changeover_count, changeover_minutes = _compute_changeovers(assignments, model)

    kpis = {
        "tardiness_minutes":  _compute_tardiness(assignments, model.products),
        "changeover_count":   changeover_count,
        "changeover_minutes": changeover_minutes,
        "makespan_minutes":   _compute_makespan(assignments),
        "utilization_pct":    _compute_utilization(assignments, model),
    }

    assert kpis["tardiness_minutes"] >= 0
    assert kpis["changeover_count"] >= 0
    assert kpis["changeover_minutes"] >= 0
    assert kpis["makespan_minutes"] >= 0
    assert all(0 <= value <= 100 for value in kpis["utilization_pct"].values())

    return kpis


# ---------------------------------------------------------------------------
# Helper: group and sort assignments by resource
# ---------------------------------------------------------------------------

def _group_assignments_by_resource(assignments: list[dict]) -> dict[str, list[dict]]:
    """
    Groups the flat assignments list into a dict keyed by resource_id.
    Each value list is sorted ascending by start time.
    Used by both _compute_changeovers and _compute_utilization.
    Edge case: empty assignments list returns an empty dict.
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    for assignment in assignments:
        grouped[assignment["resource_id"]].append(assignment)
    for resource_id in grouped:
        grouped[resource_id].sort(key=operator.itemgetter("start"))
    return dict(grouped)


# ---------------------------------------------------------------------------
# KPI: tardiness
# ---------------------------------------------------------------------------

def _compute_tardiness(assignments: list[dict], products) -> int:
    """
    Computes total tardiness in minutes: sum of max(0, completion - due) per product.
    Inputs: flat assignments list, list of InternalProduct.
    Edge case: products with no assignments contribute 0 (skipped, not penalised).
    """
    due_by_product_id = {product.id: product.due for product in products}

    completion_by_product_id: dict[str, int] = {}
    for assignment in assignments:
        product_id = assignment["product_id"]
        end_time = assignment["end"]
        current_max = completion_by_product_id.get(product_id, 0)
        completion_by_product_id[product_id] = max(current_max, end_time)

    total_tardiness = 0
    for product_id, due_time in due_by_product_id.items():
        if product_id not in completion_by_product_id:
            continue
        completion_time = completion_by_product_id[product_id]
        total_tardiness += max(0, completion_time - due_time)

    return total_tardiness


# ---------------------------------------------------------------------------
# KPI: changeover count and changeover minutes
# ---------------------------------------------------------------------------

def _compute_changeovers(assignments: list[dict], model: InternalModel) -> tuple[int, int]:
    """
    Counts changeovers and sums setup minutes between consecutive operations
    on the same resource that belong to different product families.
    Inputs: flat assignments list, InternalModel (changeover_matrix, product families).
    Edge case: missing matrix key treated as 0 setup time (no changeover counted).
    """
    family_by_product_id = {product.id: product.family for product in model.products}
    by_resource = _group_assignments_by_resource(assignments)

    changeover_count = 0
    changeover_minutes = 0

    for resource_assignments in by_resource.values():
        for index in range(len(resource_assignments) - 1):
            current_assignment = resource_assignments[index]
            next_assignment = resource_assignments[index + 1]
            family_current = family_by_product_id[current_assignment["product_id"]]
            family_next = family_by_product_id[next_assignment["product_id"]]
            changeover_key = f"{family_current}->{family_next}"
            setup_minutes = model.changeover_matrix.get(changeover_key, 0)
            if setup_minutes > 0:
                changeover_count += 1
                changeover_minutes += setup_minutes

    return changeover_count, changeover_minutes


# ---------------------------------------------------------------------------
# KPI: makespan
# ---------------------------------------------------------------------------

def _compute_makespan(assignments: list[dict]) -> int:
    """
    Computes makespan: latest end time minus earliest start time across all assignments.
    Inputs: flat assignments list.
    Edge case: empty assignments list returns 0.
    """
    if not assignments:
        return 0

    earliest_start = min(assignment["start"] for assignment in assignments)
    latest_end = max(assignment["end"] for assignment in assignments)

    return latest_end - earliest_start


# ---------------------------------------------------------------------------
# KPI: utilization per resource
# ---------------------------------------------------------------------------

def _compute_utilization(assignments: list[dict], model: InternalModel) -> dict[str, int]:
    """
    Computes per-resource utilization: processing minutes / available calendar minutes * 100.
    Inputs: flat assignments list, InternalModel (resource calendar windows).
    Changeover gaps are excluded from the numerator — only actual operation time counts.
    Edge case: resource with no assignments gets 0%. Resources absent from assignments
    but present in the model are included with 0%.
    """
    by_resource = _group_assignments_by_resource(assignments)
    utilization: dict[str, int] = {}

    for resource in model.resources:
        resource_assignments = by_resource.get(resource.id, [])
        processing_minutes = sum(
            assignment["end"] - assignment["start"]
            for assignment in resource_assignments
        )
        available_minutes = sum(
            window_end - window_start
            for window_start, window_end in resource.windows
        )
        if available_minutes == 0:
            utilization[resource.id] = 0
        else:
            utilization[resource.id] = round(processing_minutes / available_minutes * 100)

    return utilization
