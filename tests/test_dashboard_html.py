import json
import re

from nctl_core.dashboard.html import render_dashboard_html
from nctl_core.drift.engine import TargetStatus
from nctl_core.drift.model import DiffRecord, Severity, Status, Target
from nctl_core.drift_render import DriftData, DriftSourcesData
from nctl_core.output import Envelope, EnvelopeError

EMBEDDED_JSON_RE = re.compile(
    r'<script type="application/json" id="nctl-drift">(.*?)</script>', re.S
)

HOSTILE_MESSAGE = 'desired has </script><script>alert(1)</script>   in it'


def _diff(code: str, severity: Severity, message: str) -> DiffRecord:
    return DiffRecord(
        target=Target(kind="node", slug="agbad", name="agbad", id="n2"),
        code=code,
        severity=severity,
        message=message,
        desired={"mac": "aa:bb:cc:dd:ee:ff"},
        actual={},
        sources=["desired", "actual"],
    )


def _ok_envelope() -> Envelope[DriftData]:
    data = DriftData(
        generated_at="2026-07-16T12:00:00+00:00",
        summary={"converged": 1, "drifting": 1, "converging": 1, "unknown": 1},
        severity_summary={"error": 1, "warning": 0, "info": 0},
        targets=[
            TargetStatus(
                target=Target(kind="service", slug=None, name="web", id="s1"),
                status=Status.CONVERGING,
                diffs=[],
            ),
            TargetStatus(
                target=Target(kind="node", slug="agok", name="agok", id="n1"),
                status=Status.CONVERGED,
                diffs=[],
            ),
            TargetStatus(
                target=Target(kind="node", slug="agbad", name="agbad", id="n2"),
                status=Status.DRIFTING,
                diffs=[_diff("dhcp_mac_missing", Severity.ERROR, HOSTILE_MESSAGE)],
            ),
            TargetStatus(
                target=Target(kind="node", slug="agdark", name="agdark", id="n3"),
                status=Status.UNKNOWN,
                diffs=[],
            ),
        ],
        sources=DriftSourcesData(
            fetched_at="2026-07-16T12:00:00+00:00",
            observed_dump_count=3,
            observed_errors=["bad dump: agdark.json"],
        ),
    )
    return Envelope.build("nctl.drift.v1", data, [])


def _failed_envelope() -> Envelope[DriftData]:
    return Envelope.build(
        "nctl.drift.v1", DriftData(), [EnvelopeError(code="nautobot_fetch_failed", message="boom")]
    )


def _extract_embedded(html: str) -> str:
    match = EMBEDDED_JSON_RE.search(html)
    assert match is not None, "embedded drift JSON block not found"
    return match.group(1)


def test_embedded_json_round_trips():
    envelope = _ok_envelope()
    html = render_dashboard_html(envelope)

    embedded = json.loads(_extract_embedded(html))

    assert embedded == json.loads(envelope.to_json())
    assert embedded["data"]["targets"][2]["diffs"][0]["message"] == HOSTILE_MESSAGE


def test_payload_strings_cannot_close_the_script_block():
    html = render_dashboard_html(_ok_envelope())

    embedded_raw = _extract_embedded(html)

    # The hostile `</script>` in the diff message must survive only as the
    # JSON escape `<\/`, never as a literal closing tag inside the block.
    assert "</script" not in embedded_raw
    assert "<\\/script>" in embedded_raw


def test_failed_envelope_still_renders_with_errors():
    envelope = _failed_envelope()
    html = render_dashboard_html(envelope)

    embedded = json.loads(_extract_embedded(html))

    assert embedded["ok"] is False
    assert embedded["errors"][0]["code"] == "nautobot_fetch_failed"
    assert 'id="errors"' in html


def test_page_is_self_contained():
    html = render_dashboard_html(_ok_envelope())

    assert "<style>" in html
    assert "<script>" in html
    # No external assets: nothing fetched over the network, ever.
    assert "http://" not in html
    assert "https://" not in html
    assert "src=" not in html
    assert 'rel="stylesheet"' not in html


def test_all_status_styles_exist():
    html = render_dashboard_html(_ok_envelope())

    for status in ("converged", "converging", "drifting", "unknown"):
        assert f".tile.status-{status}" in html
        assert f".chip.status-{status}" in html


def test_active_placement_not_applied_renders_with_warning_severity_and_evidence():
    diff = DiffRecord(
        target=Target(kind="node", slug="agplanned", name="agplanned", id="n4"),
        code="active_placement_not_applied",
        severity=Severity.WARNING,
        message="agplanned: placement 'primary' is recorded as active but not applied because node lifecycle 'planned' is outside production scope",
        desired={
            "placement": {
                "id": "p1",
                "instance_name": "primary",
                "deployment_profile": "web",
                "config_schema_version": "1",
                "desired_state": "active",
                "config": {"enabled": True},
            }
        },
        actual={"node_lifecycle": "planned", "eligible_lifecycles": ["active", "approved"], "application_status": "not_applied"},
        sources=["desired", "actual"],
    )
    envelope = Envelope.build(
        "nctl.drift.v1",
        DriftData(
            generated_at="2026-07-16T12:00:00+00:00",
            summary={"converged": 1, "drifting": 0, "converging": 0, "unknown": 0},
            severity_summary={"error": 0, "warning": 1, "info": 0},
            targets=[
                TargetStatus(
                    target=Target(kind="node", slug="agplanned", name="agplanned", id="n4"),
                    status=Status.CONVERGED,
                    diffs=[diff],
                ),
            ],
            sources=DriftSourcesData(fetched_at="2026-07-16T12:00:00+00:00", observed_dump_count=0, observed_errors=[]),
        ),
        [],
    )

    html = render_dashboard_html(envelope)
    embedded = json.loads(_extract_embedded(html))
    rendered_diff = embedded["data"]["targets"][0]["diffs"][0]
    target_status = embedded["data"]["targets"][0]["status"]

    # A warning-only target stays converged (Decision 4): the dashboard tile
    # is still green, but the finding (message, evidence, warning severity)
    # is present in the payload the client-side JS renders as a badge/detail.
    assert target_status == "converged"
    assert rendered_diff["code"] == "active_placement_not_applied"
    assert rendered_diff["severity"] == "warning"
    assert "not applied" in rendered_diff["message"]
    assert rendered_diff["desired"]["placement"]["config"] == {"enabled": True}
    assert "<\\/" not in rendered_diff["message"]  # nothing hostile to begin with, sanity check only
