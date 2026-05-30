# OR-Tools CP-SAT scheduler. Accepts an InternalModel and returns a schedule dict
# or a structured infeasibility dict. No knowledge of input format or client specifics.

from __future__ import annotations

from typing import Any, Callable

from ortools.sat.python import cp_model

from app.models.internal import InternalModel

# (product_id, step_index) uniquely identifies one operation across the whole model.
OpKey = tuple[str, int]


def solve(model: InternalModel) -> dict:
    """
    Entry point. Builds the CP-SAT model, applies constraints, solves, and returns
    either {"status": "feasible", "assignments": [...]}
    or     {"status": "infeasible", "reasons": [...]}.
    """
    # Fast pre-solve checks produce concrete human-readable reasons before
    # spending time on the solver.
    reasons = _check_infeasibility(model)
    if reasons:
        return {"status": "infeasible", "reasons": reasons}

    cp = cp_model.CpModel()
    vars_ = _build_variables(cp, model)

    _constrain_one_resource_per_operation(cp, vars_, model)
    _constrain_precedence(cp, vars_, model)
    _constrain_no_overlap(cp, vars_, model)
    _constrain_calendars(cp, vars_, model)
    _constrain_changeovers(cp, vars_, model)
    _apply_objective(cp, vars_, model)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = model.time_limit_seconds
    solver.parameters.num_search_workers = 1  # single worker guarantees deterministic output

    status = solver.Solve(cp)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _extract_solution(solver, vars_, model)

    if status == cp_model.INFEASIBLE:
        return {
            "status": "infeasible",
            "reasons": [
                "Solver proved no feasible schedule exists — "
                "constraints are mutually contradictory given the available "
                "calendar time, changeovers, and resource capacity."
            ],
        }

    # UNKNOWN: time limit exhausted before any solution was found.
    return {
        "status": "infeasible",
        "reasons": [
            f"No solution found within the {model.time_limit_seconds}s time limit — "
            "try increasing settings.time_limit_seconds or relaxing constraints."
        ],
    }


# ---------------------------------------------------------------------------
# Pre-solve infeasibility checks
# ---------------------------------------------------------------------------

def _check_infeasibility(model: InternalModel) -> list[str]:
    """
    Run fast structural checks before building the CP model.
    Returns a list of concrete reasons; an empty list means the checks passed.
    Two cases are detected here:
      1. An operation has no eligible resource (capability not covered).
      2. An operation's duration exceeds every window on every eligible resource
         (can never fit non-preemptively).
    Subtler infeasibilities (e.g. changeover + calendar interactions) are left
    to the solver to detect and reported with a generic solver-level message.
    """
    res_by_id = {r.id: r for r in model.resources}
    reasons: list[str] = []

    for product in model.products:
        for op in product.operations:
            reasons += _check_eligibility(op)
            if not op.eligible_resources:
                continue  # window check is meaningless with no resources
            reasons += _check_window_fits(op, res_by_id)

    return reasons


def _check_eligibility(op) -> list[str]:
    """Return a reason if the operation has no eligible resource at all."""
    if op.eligible_resources:
        return []
    return [
        f"No resource has capability '{op.capability}' "
        f"(product {op.product_id}, step {op.step_index})"
    ]


def _check_window_fits(op, res_by_id: dict) -> list[str]:
    """
    Return a reason if every window on every eligible resource is shorter than
    the operation duration — meaning it can never be scheduled non-preemptively.
    """
    for res_id in op.eligible_resources:
        resource = res_by_id[res_id]
        for w_start, w_end in resource.windows:
            if w_end - w_start >= op.duration:
                return []  # at least one (resource, window) pair fits

    return [
        f"Operation {op.product_id} step {op.step_index} "
        f"({op.capability}, {op.duration} min) has no eligible resource "
        f"with a single window of at least {op.duration} min"
    ]


# ---------------------------------------------------------------------------
# Variable construction
# ---------------------------------------------------------------------------

def _build_variables(cp: cp_model.CpModel, model: InternalModel) -> dict[str, Any]:
    """
    Create one optional interval per (operation, eligible-resource) pair.

    Optional intervals let OR-Tools ignore inactive candidates automatically
    in AddNoOverlap (Stage 3). op_start / op_end are aggregate IntVars linked
    to whichever resource is chosen; all ordering constraints use these, not
    the per-resource variables.
    """
    H = model.horizon_end

    start:    dict[OpKey, dict[str, cp_model.IntVar]]      = {}
    end:      dict[OpKey, dict[str, cp_model.IntVar]]      = {}
    interval: dict[OpKey, dict[str, cp_model.IntervalVar]] = {}
    active:   dict[OpKey, dict[str, cp_model.IntVar]]      = {}
    op_start: dict[OpKey, cp_model.IntVar]                 = {}
    op_end:   dict[OpKey, cp_model.IntVar]                 = {}

    for product in model.products:
        for op in product.operations:
            key: OpKey = (op.product_id, op.step_index)
            start[key] = {}
            end[key]   = {}
            interval[key] = {}
            active[key]   = {}

            op_start[key] = cp.NewIntVar(0, H, f"op_start|{op.product_id}|{op.step_index}")
            op_end[key]   = cp.NewIntVar(0, H, f"op_end|{op.product_id}|{op.step_index}")

            for res_id in op.eligible_resources:
                tag = f"{op.product_id}|{op.step_index}|{res_id}"
                s  = cp.NewIntVar(0, H, f"start|{tag}")
                e  = cp.NewIntVar(0, H, f"end|{tag}")
                a  = cp.NewBoolVar(f"active|{tag}")
                iv = cp.NewOptionalIntervalVar(s, op.duration, e, a, f"interval|{tag}")

                start[key][res_id]    = s
                end[key][res_id]      = e
                active[key][res_id]   = a
                interval[key][res_id] = iv

                # Link aggregate variables to the chosen resource's variables.
                # op_start / op_end only equal the active resource's values;
                # inactive branches are unconstrained and ignored by the solver.
                cp.Add(op_start[key] == s).OnlyEnforceIf(a)
                cp.Add(op_end[key]   == e).OnlyEnforceIf(a)

    return {
        "start":    start,
        "end":      end,
        "interval": interval,
        "active":   active,
        "op_start": op_start,
        "op_end":   op_end,
    }


# ---------------------------------------------------------------------------
# Constraint: exactly one resource per operation
# ---------------------------------------------------------------------------

def _constrain_one_resource_per_operation(
    cp: cp_model.CpModel,
    vars_: dict[str, Any],
    model: InternalModel,
) -> None:
    """
    Enforce eligibility: each operation must be assigned to exactly one
    of its eligible resources. Without this, the solver could leave an
    operation unassigned or double-assign it.
    """
    for product in model.products:
        for op in product.operations:
            key: OpKey = (op.product_id, op.step_index)
            cp.AddExactlyOne(vars_["active"][key][res_id] for res_id in op.eligible_resources)


# ---------------------------------------------------------------------------
# Constraint: precedence within a product's route
# ---------------------------------------------------------------------------

def _constrain_precedence(
    cp: cp_model.CpModel,
    vars_: dict[str, Any],
    model: InternalModel,
) -> None:
    """
    Enforce route ordering: each step must start only after the previous
    step in the same product has finished. Uses op_end / op_start (the
    resource-agnostic aggregates) so the constraint holds regardless of
    which resource each step is assigned to.
    """
    for product in model.products:
        ops = product.operations
        for i in range(len(ops) - 1):
            prev_key: OpKey = (ops[i].product_id,     ops[i].step_index)
            next_key: OpKey = (ops[i + 1].product_id, ops[i + 1].step_index)
            cp.Add(vars_["op_start"][next_key] >= vars_["op_end"][prev_key])


# ---------------------------------------------------------------------------
# Constraint: no two operations on the same resource overlap in time
# ---------------------------------------------------------------------------

def _constrain_no_overlap(
    cp: cp_model.CpModel,
    vars_: dict[str, Any],
    model: InternalModel,
) -> None:
    """
    Enforce that each resource executes at most one operation at a time.
    Collects every optional interval variable registered to each resource
    and passes the list to AddNoOverlap. OR-Tools automatically ignores
    intervals whose active literal is False, so only the chosen assignment
    participates in the non-overlap check.
    """
    resource_intervals: dict[str, list] = {r.id: [] for r in model.resources}

    for product in model.products:
        for op in product.operations:
            key: OpKey = (op.product_id, op.step_index)
            for res_id in op.eligible_resources:
                resource_intervals[res_id].append(vars_["interval"][key][res_id])

    for res_id, intervals in resource_intervals.items():
        if intervals:
            cp.AddNoOverlap(intervals)


# ---------------------------------------------------------------------------
# Constraint: each operation must fit fully within one calendar window
# ---------------------------------------------------------------------------

def _constrain_calendars(
    cp: cp_model.CpModel,
    vars_: dict[str, Any],
    model: InternalModel,
) -> None:
    """
    Enforce non-preemption and calendar adherence: if an operation is assigned
    to a resource, its [start, end] must be fully contained within exactly one
    of that resource's calendar windows. An operation cannot span a break.

    For each (operation, resource, window) triple we create a BoolVar
    `fits_in_window`. When True it enforces start >= window.start and
    end <= window.end. Exactly one window must fit when the resource is active;
    none may fit when it is inactive (prevents spurious constraints on
    variables that won't appear in the solution).
    """
    res_by_id = {r.id: r for r in model.resources}

    for product in model.products:
        for op in product.operations:
            key: OpKey = (op.product_id, op.step_index)

            for res_id in op.eligible_resources:
                resource = res_by_id[res_id]
                a = vars_["active"][key][res_id]
                s = vars_["start"][key][res_id]
                e = vars_["end"][key][res_id]

                window_fit_vars = []
                for w_idx, (w_start, w_end) in enumerate(resource.windows):
                    tag = f"fits|{op.product_id}|{op.step_index}|{res_id}|{w_idx}"
                    f = cp.NewBoolVar(tag)
                    window_fit_vars.append(f)

                    cp.Add(s >= w_start).OnlyEnforceIf(f)
                    cp.Add(e <= w_end).OnlyEnforceIf(f)

                # Active  -> exactly one window contains the operation.
                # Inactive -> no window selected (keeps inactive vars unconstrained).
                cp.Add(sum(window_fit_vars) == 1).OnlyEnforceIf(a)
                cp.Add(sum(window_fit_vars) == 0).OnlyEnforceIf(a.Not())


# ---------------------------------------------------------------------------
# Constraint: changeover setup time between operations of different families
# ---------------------------------------------------------------------------

def _constrain_changeovers(
    cp: cp_model.CpModel,
    vars_: dict[str, Any],
    model: InternalModel,
) -> None:
    """
    For every unordered pair of operations sharing an eligible resource, if at
    least one direction of the changeover matrix is non-zero, create an ordering
    BoolVar (a_before_b / b_before_a) and enforce the required setup gap.

    Why all pairs, not just adjacent pairs: adjacency is unknown at model-build
    time — the solver decides it. Applying changeover between every pair is safe
    because non-adjacent pairs produce weaker constraints that are always
    dominated by the binding adjacent-pair constraint.

    Zero-changeover pairs (same family in both directions) are skipped entirely;
    AddNoOverlap already provides the necessary separation.
    """
    family_by_pid = {p.id: p.family for p in model.products}

    # Group (OpKey, family) by the resources they are eligible for.
    res_to_ops: dict[str, list[tuple[OpKey, str]]] = {r.id: [] for r in model.resources}
    for product in model.products:
        for op in product.operations:
            key: OpKey = (op.product_id, op.step_index)
            family = family_by_pid[op.product_id]
            for res_id in op.eligible_resources:
                res_to_ops[res_id].append((key, family))

    for res_id, op_list in res_to_ops.items():
        for i, (key_a, fam_a) in enumerate(op_list):
            for (key_b, fam_b) in op_list[i + 1:]:
                co_ab = _get_changeover(model.changeover_matrix, fam_a, fam_b)
                co_ba = _get_changeover(model.changeover_matrix, fam_b, fam_a)

                if co_ab == 0 and co_ba == 0:
                    continue  # AddNoOverlap is sufficient for same-family pairs

                _add_ordering_with_changeover(cp, vars_, key_a, key_b, res_id, co_ab, co_ba)


def _add_ordering_with_changeover(
    cp: cp_model.CpModel,
    vars_: dict[str, Any],
    key_a: OpKey,
    key_b: OpKey,
    res_id: str,
    co_ab: int,
    co_ba: int,
) -> None:
    """
    Add ordering BoolVars and conditional changeover gaps for one (op_a, op_b,
    resource) triple.

    Ordering rules:
      - Both active on res  → exactly one of {a_before_b, b_before_a} is True.
      - Either not on res   → both ordering vars forced to False so their
                              OnlyEnforceIf branches never fire.
    """
    active_a = vars_["active"][key_a][res_id]
    active_b = vars_["active"][key_b][res_id]

    tag = f"{key_a[0]}|{key_a[1]}|{key_b[0]}|{key_b[1]}|{res_id}"
    a_before_b = cp.NewBoolVar(f"a_before_b|{tag}")
    b_before_a = cp.NewBoolVar(f"b_before_a|{tag}")

    # Both active → exactly one ordering
    cp.Add(a_before_b + b_before_a == 1).OnlyEnforceIf([active_a, active_b])

    # Either inactive → both ordering vars are 0 (prevents spurious gap constraints)
    for lit in (active_a.Not(), active_b.Not()):
        cp.Add(a_before_b == 0).OnlyEnforceIf(lit)
        cp.Add(b_before_a == 0).OnlyEnforceIf(lit)

    s_a = vars_["start"][key_a][res_id]
    e_a = vars_["end"][key_a][res_id]
    s_b = vars_["start"][key_b][res_id]
    e_b = vars_["end"][key_b][res_id]

    cp.Add(s_b >= e_a + co_ab).OnlyEnforceIf(a_before_b)
    cp.Add(s_a >= e_b + co_ba).OnlyEnforceIf(b_before_a)


def _get_changeover(matrix: dict[str, int], from_family: str, to_family: str) -> int:
    """Look up setup minutes; default to 0 if the pair is not in the matrix."""
    return matrix.get(f"{from_family}->{to_family}", 0)


# ---------------------------------------------------------------------------
# Objective
# ---------------------------------------------------------------------------

# Registry: add one entry + one function to support a new objective mode.
_OBJECTIVE_REGISTRY: dict[str, Callable] = {}


def _apply_objective(
    cp: cp_model.CpModel,
    vars_: dict[str, Any],
    model: InternalModel,
) -> None:
    """Dispatch to the objective function named by model.objective_mode."""
    fn = _OBJECTIVE_REGISTRY.get(model.objective_mode)
    if fn is None:
        raise ValueError(f"Unknown objective_mode: {model.objective_mode!r}")
    fn(cp, vars_, model)


def _objective_min_tardiness(
    cp: cp_model.CpModel,
    vars_: dict[str, Any],
    model: InternalModel,
) -> None:
    """
    Minimize total tardiness: sum of max(0, completion_time - due) per product.

    Linearised with per-product IntVars bounded below by both 0 (domain) and
    (completion - due). The minimiser drives each var to the tighter bound,
    giving max(0, completion - due) without a nonlinear max() expression.
    """
    tardiness_vars = []

    for product in model.products:
        last_op = product.operations[-1]
        last_key: OpKey = (last_op.product_id, last_op.step_index)
        completion = vars_["op_end"][last_key]

        t = cp.NewIntVar(0, model.horizon_end, f"tardiness|{product.id}")
        tardiness_vars.append(t)

        # t >= completion - due  (positive part of tardiness)
        # t >= 0                 (enforced by domain; solver minimises so t->0 when on time)
        cp.Add(t >= completion - product.due)

    cp.Minimize(sum(tardiness_vars))


_OBJECTIVE_REGISTRY["min_tardiness"] = _objective_min_tardiness


# ---------------------------------------------------------------------------
# Solution extraction
# ---------------------------------------------------------------------------

def _extract_solution(
    solver: cp_model.CpSolver,
    vars_: dict[str, Any],
    model: InternalModel,
) -> dict:
    """Read solved variable values and build the assignments list."""
    assignments = []

    for product in model.products:
        for op in product.operations:
            key: OpKey = (op.product_id, op.step_index)
            for res_id in op.eligible_resources:
                if solver.Value(vars_["active"][key][res_id]):
                    assignments.append({
                        "product_id": op.product_id,
                        "step_index": op.step_index,
                        "capability":  op.capability,
                        "resource_id": res_id,
                        "start": solver.Value(vars_["start"][key][res_id]),
                        "end":   solver.Value(vars_["end"][key][res_id]),
                    })
                    break  # exactly one resource is active per operation

    return {"status": "feasible", "assignments": assignments}
