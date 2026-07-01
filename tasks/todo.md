# S4.9 — Async spot-check sampling (#27)

**AC**
1. First item per channel + random 10% flagged `spot_check=true`
2. Flagged items appear in a review queue; flagging **never blocks** publishing (async/optional)

**Design** (spot_check is an annotation set once at creation, orthogonal to status → cannot block publish)
1. `models/content_item.py`: add `spot_check: bool = Field(default=False, index=True)` (schema seam, TECH_SPEC §4).
2. `modules/crank/generate.py`: at persist time set
   `spot_check = first-item-for-channel OR sample() < 0.10`.
   - `SPOT_CHECK_RATE = 0.10`; inject `sample: Callable[[], float] = random.random` for deterministic tests.
   - `_is_first_for_channel(session, product_id, channel_id)` → no prior ContentItem row.
3. `api/private/content.py`: `GET /content/{product_id}/spot-check` → flagged items, newest first.
4. Dashboard: `getSpotCheckQueue` + `spot_check` field in `lib/api.ts`; `SpotCheckQueue` component; wire into product page.

**Tests** (`tests/test_spot_check.py`): first item always flagged; later items flag by rng; flag never changes status; API returns only flagged items newest-first.

**Skipped (YAGNI):** no "mark reviewed" state — AC says reviewing is optional/async, display-only queue. Add when an operator needs to clear the queue.
