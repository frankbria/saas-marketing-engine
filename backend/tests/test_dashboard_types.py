"""S5.2.1 (#83): the dashboard's TypeScript unions match the backend enums they mirror.

They have now drifted twice. Phase B added `ChannelType.PODCAST` and
`ContentItemStatus.RENDERING` / `RENDER_FAILED`; `dashboard/lib/api.ts` kept the Phase A sets, so
`tsc` was asserting a contract the backend does not honour — a podcast channel and a rendering
item were typed as impossible while being entirely reachable. Nothing broke visibly, because the
calendar falls back to a muted badge for unknown statuses, which is precisely why it survived two
phases unnoticed.

**Why a test and not generated types.** Generating from the OpenAPI schema
(`openapi-typescript`) would make drift structurally impossible, but it costs a dev dependency, a
generation step, a committed artifact, a freshness check in CI, and a rewrite of the hand-written
interfaces in `api.ts` into a differently-shaped generated namespace. This test buys the same
guarantee for the enums — which is where both drifts happened — at ~40 lines and no build step.
If the *interfaces* start drifting too, or this list grows past a handful, generation earns its
cost and this test is what gets deleted.

Read as text, not imported: the backend suite cannot execute TypeScript, and a regex over a union
literal is the whole contract.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models import (
    ChannelType,
    ConnectState,
    ContentItemStatus,
    LifecycleState,
    MonetizationModel,
    QaItemStatus,
)

API_TS = Path(__file__).resolve().parents[2] / "dashboard" / "lib" / "api.ts"

# TS type alias name → the backend enum it mirrors.
MIRRORED_ENUMS = {
    "MonetizationModel": MonetizationModel,
    "LifecycleState": LifecycleState,
    "ChannelType": ChannelType,
    "ConnectState": ConnectState,
    "QaItemStatus": QaItemStatus,
    "ContentItemStatus": ContentItemStatus,
}


def _union_members(source: str, alias: str) -> set[str]:
    """Extract the string-literal members of `export type <alias> = "a" | "b" | …`.

    Handles both the single-line and the leading-pipe multi-line formats prettier produces.
    """
    match = re.search(
        rf'^export type {alias} =\s*\n?((?:\s*\|?\s*"[^"]+"\s*\n?)+)', source, re.MULTILINE
    )
    assert match, f"no `export type {alias}` union found in {API_TS.name}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


@pytest.fixture(scope="module")
def api_ts() -> str:
    assert API_TS.is_file(), f"{API_TS} is missing — did the dashboard move?"
    return API_TS.read_text()


@pytest.mark.parametrize(("alias", "enum"), MIRRORED_ENUMS.items(), ids=MIRRORED_ENUMS.keys())
def test_dashboard_union_matches_the_backend_enum(api_ts, alias, enum):
    backend = {member.value for member in enum}
    dashboard = _union_members(api_ts, alias)

    assert dashboard == backend, (
        f"dashboard/lib/api.ts `{alias}` has drifted from {enum.__name__}:\n"
        f"  missing from the dashboard: {sorted(backend - dashboard) or 'none'}\n"
        f"  not in the backend enum:    {sorted(dashboard - backend) or 'none'}"
    )


def test_the_parser_would_notice_a_missing_member():
    """Guard the guard: a regex that silently matched nothing would make every case above pass."""
    source = 'export type Thing =\n  | "a"\n  | "b"\n'
    assert _union_members(source, "Thing") == {"a", "b"}
    assert _union_members('export type Thing = "a" | "b"\n', "Thing") == {"a", "b"}
    with pytest.raises(AssertionError, match="no `export type Missing` union"):
        _union_members(source, "Missing")
