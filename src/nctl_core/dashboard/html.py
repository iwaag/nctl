"""Pure dashboard renderer: an `nctl.drift.v1` envelope in, one HTML page out
(Phase 3 Step 1).

The renderer never computes drift and never fetches anything — if the page is
missing information a human needs, that is an additive `nctl.drift.v1` schema
change, not a reason for the dashboard to reach around the engine (Phase 3
Decision 1). Python's only job here is embedding the envelope JSON safely into
the template; all layout and interaction live in the template's inline JS,
which renders the embedded JSON client-side — one rendering code path, per the
Phase 0 "text is a rendering of the JSON" convention.

A failed drift envelope (`ok: false`) still renders: the page shows the run's
errors instead of silently leaving the previous generation's greens up.
"""

from __future__ import annotations

import json
from importlib import resources

from nctl_core.drift_render import DriftData
from nctl_core.output import Envelope

_JSON_PLACEHOLDER = "__NCTL_DRIFT_JSON__"


def render_dashboard_html(envelope: Envelope[DriftData]) -> str:
    template = resources.files("nctl_core.dashboard").joinpath("template.html").read_text()
    return template.replace(_JSON_PLACEHOLDER, _embeddable_json(envelope))


def _embeddable_json(envelope: Envelope[DriftData]) -> str:
    """Serialize the envelope so it can sit inside a <script> block.

    `ensure_ascii` keeps U+2028/U+2029 (legal in JSON, line terminators in JS)
    escaped; rewriting `</` as the equivalent JSON escape `<\\/` prevents any
    payload string (e.g. a hostile `</script>` in a diff message) from closing
    the script block early.
    """
    payload = json.dumps(json.loads(envelope.to_json()), ensure_ascii=True, separators=(",", ":"))
    return payload.replace("</", "<\\/")
