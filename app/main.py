# HTTP layer for the Harmony Production Scheduler.
# Receives POST /schedule, delegates to normalize → solve → compute_kpis → format,
# and returns the result. No scheduling logic, KPI math, or data transformation
# beyond converting integer minutes back to ISO datetimes lives here.

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.models.input_schema import ScheduleRequest
from app.models.output_schema import Assignment, KPIs, ScheduleResponse, InfeasibleResponse
from app.models.internal import InternalModel
from app.clients.client_a import translate as normalize
from app.scheduler.engine import solve
from app.kpis import compute_kpis


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Harmony Production Scheduler",
    description="Constraint-based production scheduling API",
    version="1.0.0",
)

# CORS required so ui/index.html can call this API directly from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

# Return type annotated as ScheduleResponse for OpenAPI docs; infeasible and
# error paths return JSONResponse directly to control the HTTP status code.
@app.post("/schedule")
def schedule(request: ScheduleRequest) -> ScheduleResponse:
    """POST /schedule — validates input, runs the solver, returns a schedule or error."""
    try:
        internal_model = normalize(request)
        result = solve(internal_model)

        if result["status"] == "infeasible":
            return JSONResponse(
                status_code=422,
                content={"error": "infeasible", "why": result["reasons"]},
            )

        kpis = compute_kpis(result["assignments"], internal_model)
        return _format_response(result["assignments"], kpis, internal_model, request.horizon.start)

    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "message": str(exc)},
        )


# ---------------------------------------------------------------------------
# Response formatter
# ---------------------------------------------------------------------------

def _format_response(
    assignments: list[dict],
    kpis: dict,
    model: InternalModel,
    origin: datetime,
) -> ScheduleResponse:
    """
    The only place where integer-minute offsets are converted back to wall-clock
    datetimes. origin is request.horizon.start — the epoch the solver used.
    model is accepted for future formatters that may need resource metadata.
    """
    formatted_assignments = [
        Assignment(
            product=assignment["product_id"],
            step_index=assignment["step_index"],
            capability=assignment["capability"],
            resource=assignment["resource_id"],
            start=origin + timedelta(minutes=assignment["start"]),
            end=origin + timedelta(minutes=assignment["end"]),
        )
        for assignment in assignments
    ]

    kpi_obj = KPIs(
        tardiness_minutes=kpis["tardiness_minutes"],
        changeover_count=kpis["changeover_count"],
        changeover_minutes=kpis["changeover_minutes"],
        makespan_minutes=kpis["makespan_minutes"],
        utilization_pct=kpis["utilization_pct"],
    )

    return ScheduleResponse(assignments=formatted_assignments, kpis=kpi_obj)


# ---------------------------------------------------------------------------
# Dev server entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
