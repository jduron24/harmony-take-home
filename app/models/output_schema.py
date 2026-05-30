# Pydantic models that define the API response contract.
# Covers both the success shape and the structured infeasible/error shape.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Assignment(BaseModel):
    product: str
    step_index: int          # 1-based position in the product's route
    capability: str
    resource: str
    start: datetime
    end: datetime


class KPIs(BaseModel):
    tardiness_minutes: int
    changeover_count: int
    changeover_minutes: int
    makespan_minutes: int
    utilization_pct: dict[str, int]  # resource_id -> whole-number percentage


class ScheduleResponse(BaseModel):
    assignments: list[Assignment]
    kpis: KPIs


class InfeasibleResponse(BaseModel):
    error: str               # short machine-readable label
    why: list[str]           # one or more concrete human-readable reasons
