# S4.7 — Retract a published item (#25)

**Story:** S4.7 · Refs USER_STORIES S4.7, TECH_SPEC §7, PRD FR-26 · Depends on S4.5.
Kill switch (S4.6) only stops *future* posts; retract pulls a bad *live* one.

## Acceptance criteria
- [x] Adapter `delete(external_url)` implemented per channel — **already pre-seeded in S4.5**
      (`BlogAdapter.delete` unlinks the file; `RedditAdapter.delete` calls `submission.delete()`).
- [ ] Dashboard "retract" action → `content_item.status = retracted` + removes remote post where API allows.

## Pre-seeded (grep-first per S4.6 lesson)
- `ContentItemStatus.RETRACTED` already in the enum + `_TERMINAL_FAILURE` set.
- Both adapters already implement `delete(...)` with the same transient/permanent split as `publish`.
- Missing: the retract **action** (engine fn + endpoint) and the dashboard **UI** to invoke it.
  No content-item API or UI exists yet, so a minimal published-items list is needed for the button.

## Plan (mirror S4.5 publish + S4.6 pause pattern)

### Backend
1. `backend/app/modules/crank/retract.py` — `retract_item(session, item, now, *, adapter_for=get_adapter)`:
   - Load channel + product; get adapter + decrypted creds (same seam as `publish_scheduled`).
   - Call `adapter.delete(item.external_url, product, channel, creds)`.
   - On success: `status = RETRACTED`, `published_at` kept as history, clear `error`, commit.
   - `Retryable`/permanent errors propagate to the caller (retract is operator-synchronous — surface,
     don't silently retry). Item stays `published` on failure so the operator can retry.
2. `backend/app/api/private/content.py` — new router `/content`:
   - `GET /{product_id}` → published + retracted items (desc by published_at) for the dashboard list.
   - `POST /{product_id}/{item_id}/retract` → 404 if not this product's item; 409 unless `PUBLISHED`;
     call `retract_item`; map `Retryable` → 503; return the updated item.
   - Register in `api/private/__init__.py`.

### Dashboard
3. `dashboard/lib/api.ts` — `ContentItem` type + `ContentItemStatus`; `listPublishedContent(pid)`,
   `retractContent(pid, itemId)`.
4. `dashboard/app/products/[id]/published-content.tsx` — list published items, Retract button
   (mirror `channel-setup.tsx` `run()` busy/error pattern). Mount on the product page.
5. `dashboard/lib/api.test.ts` — cover the two new client fns.

### Tests (backend, real DB, no mocks)
6. `backend/tests/test_retract.py` — retract sets `RETRACTED` + calls `delete(external_url)`;
   stub adapter records the delete; `Retryable` propagates (item stays published).
7. Add a `RedditAdapter.delete` network-error → `Retryable` test (delete path currently untested).
8. `backend/tests/test_content_api.py` — GET lists published; POST retract 200; non-published 409;
   missing `external_url` 409; wrong product 404; orphaned channel 409; transient adapter failure 503.

## Quality gate
- `uv run pytest` (backend) + dashboard `npm test`; ruff/black; `codex review` pre-PR.
- Demo: publish an item via stub, retract via API, assert status + remote gone.
