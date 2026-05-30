# harmony-scheduler

Constraint-based production scheduling API. Accepts machines, products, deadlines, and constraints as JSON; returns an optimized schedule and KPIs.

**Stack:** Python · FastAPI · OR-Tools CP-SAT · pytest

---

## Setup

Requires Python 3.13+. Developed and tested on Python 3.14.4.              

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run the server

```bash
source venv/bin/activate
python -m uvicorn app.main:app --reload --port 8000
```

Interactive docs: http://localhost:8000/docs

## Open the UI

```bash
open ui/index.html                # macOS
# or double-click ui/index.html in your file manager
```

The UI displays an interactive Gantt chart of the resulting schedule with one row per resource and color-coded blocks per product. Start the server first.

## Run tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```

13 tests across four files:

| File | What it tests |
|---|---|
| `test_overlap.py` | No-overlap and precedence invariants |
| `test_kpis.py` | Independent KPI recomputation from raw output |
| `test_infeasible.py` | Structured 422 error responses — two pre-solve cases and one solver-level infeasibility case |
| `test_schedule_validity.py` | Changeover gaps match matrix values, assignments fit within calendar windows, determinism (identical input → identical output) |

example.json contains the Client A sample input from the spec.

## Call the API directly

```bash
curl -s -X POST http://localhost:8000/schedule \
  -H "Content-Type: application/json" \
  -d @example.json | python3 -m json.tool
```

---

## Infeasible response

If no valid schedule exists the API returns HTTP 422 with this shape:

```json
{
  "error": "infeasible",
  "why": [
    "No resource has capability 'pack' (product P-100, step 3)"
  ]
}
```

The `why` list contains at least one concrete reason naming the specific product, step, and constraint that failed.

---

## Approach

**Why FastAPI**

FastAPI is a framework I work with regularly, so I could focus on the scheduling problem rather than the HTTP layer. It also pairs naturally with Pydantic, request validation, response serialization, and the auto-generated `/docs` UI all come for free from the same model definitions used in the code. For a project where the input and output schemas are the contract, that tight integration keeps things clean.

**Why CP-SAT over a heuristic**

CP-SAT guarantees all constraints are satisfied and finds the optimal solution automatically. A heuristic guarantees neither. CP-SAT's interval variable primitives are purpose-built for scheduling problems, making the constraint model direct and readable.


**Solver status meanings**

| Status | Meaning |
|---|---|
| `OPTIMAL` | Minimum tardiness proven; no better solution exists |
| `FEASIBLE` | A valid schedule was found within the time limit; optimality not proven |
| `INFEASIBLE` | Solver proved no schedule satisfying all constraints exists |
| `UNKNOWN` | Time limit exhausted before any solution was found |

---

## Assumptions and tradeoffs

**Integer-minute time resolution.** All times are converted to integer minutes from `horizon.start` before entering the solver. Sub-minute durations are not supported. This simplifies the CP-SAT model and is consistent with how production schedules are specified in practice.

**All-pairs changeover modeling.** Changeover constraints are applied between every pair of operations that share an eligible resource, not just adjacent pairs. Non-adjacent pairs produce dominated (weaker) constraints that are always satisfied if the binding adjacent-pair constraint is satisfied. This avoids having to determine adjacency at model-build time, which is a runtime decision made by the solver.

**Pre-solve infeasibility checks.** Two structural checks run before the CP model is built: (1) every operation has at least one eligible resource, (2) every operation fits in at least one calendar window of at least one eligible resource. These produce concrete, named reasons immediately rather than waiting for the solver to time out.

**Solver over heuristic.** OR-Tools CP-SAT was chosen over a hand-written greedy algorithm because the four constraint types interact in ways that make locally optimal decisions globally wrong. For example, a changeover can push an operation past a calendar window which cascades into downstream deadline misses. CP-SAT handles all constraints simultaneously and guarantees the solution respects every rule. The tradeoff is debuggability, when the solver returns an unexpected result it is harder to trace than stepping through custom code. At the current problem size this cost is acceptable.

**Determinism.** `num_search_workers = 1` is set explicitly in the solver. By default CP-SAT uses all available cores and runs multiple worker threads in parallel; whichever thread finds the optimal solution first wins, and that varies with CPU scheduling. The result is correct either way but the specific schedule differs between calls. Pinning to one worker removes the race and guarantees identical output for identical input. The tradeoff is slower time-to-first-solution on large problems, negligible here at ~4ms, but worth removing if the problem size grows significantly.

---

## Design notes

**Request flow**

`POST /schedule` → Pydantic validates body → `client_a.translate()` converts to `InternalModel` → `engine.solve()` runs CP-SAT → `kpis.compute_kpis()` calculates statistics → `_format_response()` converts integer minutes back to ISO datetimes → `ScheduleResponse` returned.

**Internal model**

`InternalModel` (`app/models/internal.py`) is the contract between clients and the solver. All times are integer minute offsets from `horizon.start` (minute 0). Fields: `horizon_end`, `products` (with `operations` carrying `eligible_resources`), `resources` (with `windows` as `(start, end)` tuples), `changeover_matrix`, `time_limit_seconds`, `objective_mode`. The solver imports only this model, never the request schema.

**Adding a second input format**

Add `app/clients/client_b.py` with a `translate(request) -> InternalModel` function. Wire it into `main.py`. No other files change. `client_a.py` is untouched.

**Adding a new objective**

Add one function `_objective_<name>(cp, vars_, model)` in `engine.py` and register it:

```python
_OBJECTIVE_REGISTRY["<name>"] = _objective_<name>
```

The dispatcher `_apply_objective()` and `solve()` require no changes. The caller passes `"objective_mode": "<name>"` in the request.

**Adding a new constraint**

Add one function `_constrain_<name>(cp, vars_, model)` in `engine.py` and call it inside `solve()` after the existing constraint calls. Each constraint is isolated in its own function with no logic in `solve()` itself.
# harmony-take-home
