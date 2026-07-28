"""Pure MAC normalization shared by drift evaluation and dnsmasq rendering."""

from __future__ import annotations

import re
from typing import Any


def normalize_mac(value: Any) -> str:
    text = re.sub(r"[^0-9A-Fa-f]", "", "" if value is None else str(value).strip())
    if len(text) != 12:
        return ""
    return ":".join(text[index : index + 2].lower() for index in range(0, 12, 2))
