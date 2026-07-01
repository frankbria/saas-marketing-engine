# S4.8.1 — Reddit connect/adapter credential-shape mismatch (#64)

**Story:** S4.8.1 · Refs TECH_SPEC §7/§9, follow-up to S2.6 (#14) + S4.8 (#26)
**Branch:** `fix/issue-64-reddit-credential-shape`
**Plan source:** self-authored (issue had acceptance criteria, no step-by-step plan)

## Problem
`/connect` stores `payload.access_token` (a bare string) under `reddit_oauth`, but
`RedditAdapter._parse_creds` requires a JSON object of PRAW kwargs. A Reddit channel
connected via the documented flow fails at publish (`reddit_oauth credential is not valid
JSON`). The only shape that works today is an undocumented JSON blob pasted into `access_token`.

## Design decision (autonomous — no architectural fork)
The **documented Reddit credential shape is the PRAW-kwargs JSON object** the adapter already
consumes. Express it explicitly in the `/connect` request as a typed sub-model. Route by a new
`SELF_MANAGED_TYPES` constant (mirrors existing `AUTONOMOUS_TYPES`) rather than a magic
`== REDDIT` literal. Self-managed creds are stored as one JSON blob under `{type}_oauth`, with
no separate `_oauth_refresh` cred and no expiry (PRAW self-refreshes) — which keeps
`is_self_managed_credential` classifying them as self-managed (AC4). Owned bare-token path is
unchanged.

## Steps
1. **models/channel.py** — add `SELF_MANAGED_TYPES = frozenset({ChannelType.REDDIT})`.
2. **api/private/channels.py** — add `RedditCredential` typed model (`client_id`,
   `client_secret`, `refresh_token`, `user_agent`); add optional `reddit` field to
   `ConnectRequest`; branch `connect_channel` on `SELF_MANAGED_TYPES` (store
   `reddit.model_dump_json()` under `{type}_oauth`, 400 if missing). Update the deferral note.
3. **channels/reddit.py** — no logic change (already parses PRAW-kwargs JSON); leave as-is.
4. **Tests (test_channels_api.py)** — RED first:
   - `test_connect_reddit_stores_praw_kwargs` (structured creds → `reddit_oauth` parseable JSON dict)
   - `test_connect_reddit_missing_creds_400`
   - keep an owned bare-token path test (non-self-managed type, e.g. `x`)
   - `test_connect_reddit_then_publish_end_to_end` (real vault + fake PRAW → permalink) — AC3
5. **Dashboard** (bug-ownership + lessons.md "check frontend callers") — `api.ts` ConnectRequest
   type + `channel-setup.tsx` Reddit form collects the 4 PRAW fields; update `api.test.ts`.

## Acceptance criteria
- [ ] One documented, consistent Reddit credential shape across `/connect` + adapter
- [ ] `/connect` expresses that shape explicitly (typed fields, not ambiguous `access_token: str`)
- [ ] Channel connected via documented flow publishes end-to-end (real vault + fake PRAW test)
- [ ] `is_self_managed_credential` / proactive-refresh routing stays correct
- [ ] S4.8 fail-safe (`AuthFailure` fencing) intact

## Test strategy
Backend: pytest from `backend/` (real vault, fake PRAW via `_build_reddit` monkeypatch) — AC1–AC4.
Frontend: vitest `api.test.ts` covers the new request body.
