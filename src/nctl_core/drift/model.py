"""The diff record: the stable Phase 3/4 interface (Phase 2 Step 3).

`Target.kind` is deliberately a plain string rather than a closed enum — the
roadmap names `node`/`service` as the primary targets, but a comparator may
also need to report a diagnostic that isn't scoped to one desired node or
service (a global production-composition contract error, or an ingest-lag
diff for an observed dump with no matching desired node yet). Closing this
set now would force a schema break the first time that need shows up; the
`code` vocabulary is where stability actually matters (Phase 4 reconcilers
will map `code` -> playbook).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Status(str, Enum):
    CONVERGED = "converged"
    DRIFTING = "drifting"
    CONVERGING = "converging"
    UNKNOWN = "unknown"


class Target(BaseModel):
    kind: str
    slug: str | None = None
    name: str | None = None
    id: str | None = None


class DiffRecord(BaseModel):
    target: Target
    code: str
    severity: Severity
    message: str
    desired: dict = {}
    actual: dict = {}
    sources: list[str] = []
