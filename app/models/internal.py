# Solver-neutral internal representation of a scheduling problem.
# All times are integer minutes offset from horizon start (minute 0).
# Clients translate into this model; the solver consumes it. No client logic here.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class InternalOperation:
    product_id: str
    step_index: int              # 1-based position in product route
    capability: str
    duration: int                # minutes
    eligible_resources: List[str]


@dataclass
class InternalProduct:
    id: str
    family: str
    due: int                     # minutes from horizon start
    operations: List[InternalOperation]


@dataclass
class InternalResource:
    id: str
    windows: List[Tuple[int, int]]   # (start_min, end_min), inclusive start / exclusive end


@dataclass
class InternalModel:
    horizon_end: int                  # minutes from horizon start
    products: List[InternalProduct]
    resources: List[InternalResource]
    changeover_matrix: Dict[str, int] # "family_a->family_b" -> setup minutes
    time_limit_seconds: int = 30
    objective_mode: str = "min_tardiness"
