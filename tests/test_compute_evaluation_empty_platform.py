from datetime import datetime, timezone

from nctl_core.compute.model import DesiredComputePlatform
from nctl_core.drift.compute_evaluation import evaluate_compute
from nctl_core.drift.context import DriftContext
from nctl_core.sources.actual import ActualSnapshot
from nctl_core.sources.desired import DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot


def test_empty_desired_platform_is_reported_without_asserting():
    snapshot = SourceSnapshot(
        desired=DesiredSnapshot(compute_platforms=[DesiredComputePlatform(id="p1", name="p", slug="p", lifecycle="active", control_node_id="n1")]),
        actual=ActualSnapshot(), fetched_at=datetime.now(timezone.utc),
    )
    records = list(evaluate_compute(snapshot, DriftContext(generated_at=datetime.now(timezone.utc).isoformat())))
    assert [record.code for record in records] == ["compute_realization_summary"]
