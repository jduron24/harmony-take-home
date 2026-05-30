# Translates a validated ScheduleRequest (Client A's input format) into an InternalModel.
# All datetime arithmetic and unit conversion lives here.
# The solver never imports this module — add clients/client_b.py for a second source format.

from __future__ import annotations

from collections import defaultdict

from app.models.input_schema import ScheduleRequest
from app.models.internal import (
    InternalModel,
    InternalOperation,
    InternalProduct,
    InternalResource,
)


def translate(request: ScheduleRequest) -> InternalModel:
    """Convert a ScheduleRequest into the solver's integer-minute representation."""
    epoch = request.horizon.start
    horizon_end = _to_minutes((request.horizon.end - epoch).total_seconds())

    # Build capability -> [resource_id] map first; operations reference it.
    cap_to_resources = _build_cap_map(request)

    resources = [_translate_resource(r, epoch) for r in request.resources]
    products = [
        _translate_product(p, epoch, cap_to_resources) for p in request.products
    ]

    return InternalModel(
        horizon_end=horizon_end,
        resources=resources,
        products=products,
        changeover_matrix=dict(request.changeover_matrix_minutes.values),
        time_limit_seconds=request.settings.time_limit_seconds,
        objective_mode=request.settings.objective_mode,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _to_minutes(seconds: float) -> int:
    return int(seconds // 60)


def _build_cap_map(request: ScheduleRequest) -> dict[str, list[str]]:
    """Pre-compute capability -> [resource_id] so operations know their eligible resources."""
    mapping: dict[str, list[str]] = defaultdict(list)
    for resource in request.resources:
        for cap in resource.capabilities:
            mapping[cap].append(resource.id)
    return dict(mapping)


def _translate_resource(resource, epoch) -> InternalResource:
    windows = [
        (
            _to_minutes((w[0] - epoch).total_seconds()),
            _to_minutes((w[1] - epoch).total_seconds()),
        )
        for w in resource.calendar
    ]
    return InternalResource(id=resource.id, windows=windows)


def _translate_product(
    product,
    epoch,
    cap_to_resources: dict[str, list[str]],
) -> InternalProduct:
    due_minutes = _to_minutes((product.due - epoch).total_seconds())
    operations = [
        InternalOperation(
            product_id=product.id,
            step_index=i + 1,                          # 1-based
            capability=step.capability,
            duration=step.duration_minutes,
            eligible_resources=cap_to_resources.get(step.capability, []),
        )
        for i, step in enumerate(product.route)
    ]
    return InternalProduct(
        id=product.id,
        family=product.family,
        due=due_minutes,
        operations=operations,
    )
