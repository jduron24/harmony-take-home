# Pydantic models that validate and parse the raw API request body.
# Nothing here knows about the solver or internal representation — pure input contract.

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


class Horizon(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def end_after_start(self) -> "Horizon":
        if self.end <= self.start:
            raise ValueError("horizon.end must be after horizon.start")
        return self


class RouteStep(BaseModel):
    capability: str
    duration_minutes: Annotated[int, Field(gt=0)]


class Product(BaseModel):
    id: str
    family: str
    due: datetime
    route: Annotated[list[RouteStep], Field(min_length=1)]


class Resource(BaseModel):
    id: str
    capabilities: Annotated[list[str], Field(min_length=1)]
    # Each window is [start, end] as a two-element list of datetimes.
    calendar: Annotated[list[list[datetime]], Field(min_length=1)]

    @model_validator(mode="after")
    def windows_are_valid(self) -> "Resource":
        for window in self.calendar:
            if len(window) != 2:
                raise ValueError(
                    f"Resource '{self.id}': each calendar window must have exactly 2 datetimes"
                )
            if window[1] <= window[0]:
                raise ValueError(
                    f"Resource '{self.id}': calendar window end must be after start"
                )
        return self


class ChangeoverMatrix(BaseModel):
    # Keys are "family_a->family_b"; values are integer minutes.
    values: dict[str, int]


class Settings(BaseModel):
    time_limit_seconds: Annotated[int, Field(gt=0)] = 30
    objective_mode: str = "min_tardiness"


class ScheduleRequest(BaseModel):
    horizon: Horizon
    resources: Annotated[list[Resource], Field(min_length=1)]
    changeover_matrix_minutes: ChangeoverMatrix
    products: Annotated[list[Product], Field(min_length=1)]
    settings: Settings = Field(default_factory=Settings)
