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
