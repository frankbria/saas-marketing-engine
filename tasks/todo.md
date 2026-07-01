# S4.8.2 — Per-provider OAuth authorize→callback + client-cred seeding + refresh registration (#65)

Branch: `feat/s4.8.2-oauth-redirect-flow`

## Design (adapted to codebase — verified)
- Registry `OWNED_TOKEN_PROVIDERS: dict[ChannelType, OAuthProvider]` in `oauth_refresh.py`
  (authorize_url, token_url, scopes). `TOKEN_ENDPOINTS` derived from it → existing refresh path
  picks up any registered provider. Ships **empty of live providers** (X/IG/YT out of scope);
  machinery tested via `monkeypatch.setitem`. Keeps the empty-TOKEN_ENDPOINTS invariant.
- `authorization_code` exchange behind a `_post_token_exchange` urllib seam (mirrors
  `_post_token_refresh`), reuses `parse_token_response`; returns (access, refresh?, expires_at?).
- Signed/expiring `state` via Fernet (reuse `vault._fernet()`), TTL-checked on callback. No new table.
- `config.py`: `oauth_redirect_base_url` (build provider redirect_uri → callback) +
  `dashboard_base_url` (post-callback browser return). Plain `str`, SME_ prefix, localhost defaults.

## Steps
- [ ] P1 backend core: registry + TOKEN_ENDPOINTS derivation, code-exchange seam, state helper, config
- [ ] P1 tests: registry/exchange/state round-trip + expiry reject; fail-safe fence+alert (caplog)
- [ ] P2 endpoints: shared `_mark_connected` (tokens + connect_state + oauth checklist DONE);
      `POST /credentials` seeding; `GET /authorize` redirect; `GET /callback` handler
- [ ] P2 tests: seed encrypts; authorize redirect has scopes/redirect_uri/state; callback stores
      tokens + flips state + checklist done + redirects; tampered/expired state rejected
- [ ] P3 dashboard: client-cred seed form + "Connect" full-page nav to authorize; api.ts helpers
- [ ] Quality gate: ruff/black/mypy, pytest, deslop, codex review; PR; demo; merge

## Acceptance criteria (from #65)
- [ ] authorize→callback completes OAuth, writes channel-scoped tokens, connect_state=connected
- [ ] client creds seeded via flow (not hand-loaded)
- [ ] owned-token providers registered in TOKEN_ENDPOINTS → proactive refresh runs; fail-safe fires
- [ ] secrets never logged (vault redaction), tokens encrypted at rest

## Lessons carried in (from tasks/lessons.md)
- Moving connect/checklist behaviour → grep dashboard for old-endpoint callers, update flow+copy same PR.
- Run the diff past a cross-family (codex) review before merge.
