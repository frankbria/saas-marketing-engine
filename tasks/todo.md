# S4.8 — OAuth refresh handling (fail-safe) · Issue #26

**Story:** S4.8 · Refs USER_STORIES S4.8, TECH_SPEC §7/§9/§8.4, PRD FR-34
**Branch:** `feature/issue-26-oauth-refresh-handling`
**Plan source:** self-authored (no plan on the issue)

## Acceptance criteria
- [ ] Proactive refresh before token expiry
- [ ] On refresh failure → channel `failed`, halt its publishes, fire alert (S6.2)

## Design decisions (self-authored)
- **Guard on `connect_state == FAILED`, not `== CONNECTED`.** The AC only requires *failed* channels
  to halt. Requiring CONNECTED would halt blog/pending channels and break the existing S4.5/S4.6
  suite (channels default to PENDING). FAILED-only is AC-faithful and non-breaking.
- **Refresh-at-publish, mirroring the S4.6 `paused` check.** The token is only used at publish time,
  so refreshing right before publish (when within a buffer of expiry) is the proactive point. No
  separate periodic sweep (YAGNI — nothing else consumes the token between publishes).
- **Injectable `refresh=` seam** on `publish_scheduled`, mirroring the existing `adapter_for=` seam,
  so tests drive the full pass network-free (no-mocking house rule).
- **Alert = minimal log choke-point** (`app/modules/alerts.py::raise_alert`). S6.2 (heartbeat digest
  + delivery) is deferred; this is the single seam it will extend. Structured WARNING is grep-able
  and testable via caplog.
- **Default refresher** does a generic OAuth2 `refresh_token` grant (stdlib `urllib`) against a
  per-type token endpoint, reading refresh token + client creds from the vault. Pure parts
  (`needs_refresh`, `parse_token_response`) are unit-tested; the thin HTTP call is ops wiring
  exercised only against the real provider.

## Steps (TDD)
1. `vault.get_credential_expiry()` — return latest credential's `expires_at` (or None).
2. `app/modules/alerts.py` — `raise_alert(kind, message, **context)` → structured WARNING log.
3. `app/modules/crank/oauth_refresh.py` — `REFRESH_BUFFER`, `needs_refresh`, `parse_token_response`,
   `refresh_channel_token` (default), `TOKEN_ENDPOINTS`.
4. `publish_scheduled` — add `connect_state == FAILED` to the pre-publish guard; add proactive
   refresh (via injectable `refresh=`) for OAuth channels near expiry; on refresh failure set FAILED
   + alert + leave item `scheduled`.
5. `pace_content` + `crank._run_crank` — exclude `connect_state == FAILED` from channel selection
   (consistency with `~paused`; avoids generating for a dead channel).
6. Tests: FAILED channel halts publish (stays scheduled); near-expiry triggers refresh then
   publishes with refreshed creds; refresh failure → FAILED + halt + alert fired; not-near-expiry /
   no-expiry → no refresh; pure-helper unit tests for `needs_refresh` + `parse_token_response`.

## Known limitations (for PR body)
- Full per-provider authorize→callback OAuth redirect + client-credential seeding UI remain deferred
  (already deferred by the connect endpoint's own note). Ops seeds client creds via the vault.
- Reddit-via-PRAW stores `reddit_oauth` as a structured PRAW-kwargs blob and self-refreshes its
  access token under the hood, so proactive refresh **skips** self-managed (JSON) credentials.
  A dead *self-managed* refresh token is caught at publish time: the adapter raises `AuthFailure`
  (401/403/OAuth), which the publish pass turns into the same channel-level fence (`failed` + alert),
  so AC-2 holds for the real provider — not just bare-token stubs.
- Owned bare-token credentials (the `/connect` shape) refresh via a real OAuth2 refresh_token grant
  (`refresh_channel_token`), keyed by `TOKEN_ENDPOINTS` + vault client creds. `TOKEN_ENDPOINTS` is
  empty in v1 (Reddit is self-managed; blog has no OAuth) — it's the provider-registration seam. An
  unregistered near-expiry bare token is a *config gap* (`RefreshUnavailable`), not a failure: we
  proceed and let the reactive `AuthFailure` fence catch it if the token is actually dead, rather
  than halting a possibly-live channel. Full authorize→callback OAuth redirect UI remains deferred.
