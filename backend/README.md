# SME Backend

FastAPI backend for the SaaS Marketing Engine. Two API surfaces (TECH_SPEC §1):

- **private** (`/api/private/*`) — dashboard/operator API, firewalled, no auth in v1
- **public** (`/api/public/*`) — funnel-ingest (visit/lead/checkout) + Stripe webhook, internet-facing

## Develop

```bash
uv sync                       # install (Python 3.13, pinned in .python-version)
uv run uvicorn app.main:app --reload --port 8010
uv run pytest                 # tests
uv run ruff check . && uv run black --check .   # lint + format
```

Module skeleton under `app/` (`modules/{strategy,setup,qa,crank,metrics}`, `channels/`, `ai/`,
`secrets/`) is populated by later phase issues.

### Storage + jobs (S0.2)

- `db.py` — SQLModel engine on SQLite (WAL + `busy_timeout`); `init_db()` bootstraps the schema
  (no Alembic in v1). Postgres swap-in is a `SME_DATABASE_URL` change — every SQLite-ism is
  dialect-gated (CI exercises the Postgres path); see `infra/POSTGRES_MIGRATION.md` for the
  cutover procedure and when it's actually warranted.
- `models/job_run.py` — `JobRun` audit row with an `attempts` retry column.
- `worker.py` — in-process worker loop: `enqueue()` + `run_due_jobs()` (retries failures up to
  `MAX_ATTEMPTS`) + `reclaim_running_jobs()` (recovers jobs orphaned by a crash on startup).
- `scheduler.py` — APScheduler `BackgroundScheduler` (heartbeat enqueues a noop, worker tick
  drains the queue), started from the app lifespan.

### Product registry (S0.3)

- `models/product.py` — `Product`, the first-class unit (TECH_SPEC §4). All product-specific
  config (repo, domain, pricing, brand, budget) lives on this row (PRD G7). Defaults:
  `monetization_model=cc_sub`, `lifecycle_state=draft`.
- `api/private/products.py` — CRUD at `/api/private/products` (POST/GET/PATCH/DELETE).
  `lifecycle_state` is not raw-editable here; transitions go through the state machine in
  later phases (S1.4 / S3.2).
- `workspace.py` — on create, scaffolds `{SME_WORKSPACE_ROOT}/{slug}/vault/` (the empty
  credentials vault; Fernet encryption lands in S0.4). `SME_WORKSPACE_ROOT` defaults to
  `./workspace`.
- `SME_CORS_ORIGINS` (comma-separated; default `http://localhost:3010`) — browser origins
  allowed to call the private API.

### Credentials vault (S0.4)

- `models/credential.py` — `Credential` (TECH_SPEC §4): only Fernet `ciphertext` at rest,
  scoped by `(product_id, key, channel_id)`; `__repr__` omits the ciphertext.
- `secrets/vault.py` — `encrypt`/`decrypt` + `put_credential`/`get_credential`. Symmetric
  Fernet key from env **`SME_VAULT_KEY`** (a `SecretStr`, never stored in the DB). Generate one
  (run from `backend/` so the `app` package resolves):
  `cd backend && uv run python -c "from app.secrets.vault import generate_key; print(generate_key())"`,
  then put it in `backend/.env` (copy `backend/.env.example` to start).
  `install_redaction()` (wired into the app lifespan) installs a global log-record factory
  that scrubs every vault secret from all logs; `tests/test_no_plaintext_logging.py` is the
  static backstop. Single global key for v1 (per-product keys deferred — §9).

### Strategy brief (S1.1)

- `models/strategy_brief.py` — `StrategyBrief` (1:1 product, TECH_SPEC §4/§5): ICP, pain
  points, positioning, channel plan, content pillars, cadence, `raw_ai_output`.
- `modules/strategy/ingest.py` — bounded repo ingest (README/manifests/docs + route & UI/copy
  source; dot-dirs and symlink escapes excluded). Local clone preferred; `repo_url` is
  re-cloned fresh each run.
- `ai/client.py` + `ai/pricing.py` — Anthropic SDK: Haiku per-file summaries, Opus 4.8
  synthesis via structured outputs; per-call cost in cents. Needs env **`SME_ANTHROPIC_API_KEY`**
  (a `SecretStr`).
- `modules/strategy/brief.py` — `@handler("strategy_brief")`: budget-gated (pre-check +
  mid-loop cap + synthesis reservation vs `token_budget_cents_month`, `0`=unlimited) ingest →
  summarize → synthesize → upsert brief → product → `strategy`. Cost is recorded to `job_run`.
- `modules/strategy/brand.py` — `@handler("brand_kit")`: budget-gated Opus call grounded in the
  product's brief → folds a Brand Kit (name, tone, structured voice descriptors, visual seeds)
  onto `product.brand_json`. No new table, no lifecycle change. Cost recorded to `job_run`.
- `modules/strategy/pricing.py` — `@handler("pricing")` (S1.3): budget-gated Opus call grounded
  in the brief → folds a `cc_sub` price (`price_amount_cents` + `price_interval`, month/year) onto
  the product. Refused for non-`cc_sub` products (trial/freemium unwired in v1). Owner-editable via
  the products PATCH. No new table, no lifecycle change. Cost recorded to `job_run`.
- `api/private/strategy.py` — `POST /api/private/strategy/{product_id}/brief`, `.../brand`, and
  `.../pricing` enqueue their jobs (202; `brand`/`pricing` 400 until a brief exists, `pricing` also
  400s for non-`cc_sub`). The real-API integration tests are gated on `SME_ANTHROPIC_API_KEY`.
- `modules/setup/site.py` — `@handler("setup_site")` (S2.1): budget-gated Opus call writes on-brand
  landing copy + design tokens from `product.brand_json`, renders the one maintained
  `site-template/index.html.j2` (Jinja2, autoescaped), statically exports it to
  `{workspace}/{slug}/site/`, and deploys it under `marketing_domain` (`SME_NGINX_SITES_ROOT/{domain}/`
  + an emitted vhost). The generated site's funnel JS calls the public API at
  `SME_PUBLIC_API_BASE_URL` — UTM→first-touch cookie, `visit` on load, `lead` on submit, checkout
  carrying `client_reference_id`. `marketing_domain` is hostname-validated before any filesystem use.
- `api/private/setup.py` — `POST /api/private/setup/{product_id}/site` enqueues the site build (202;
  404 missing, 409 unless `setup_ready`, 400 until a brand kit exists).
- `integrations/stripe_api.py` (S2.3) — stdlib-only Stripe REST calls (Product, recurring Price,
  Checkout Session); no `stripe` SDK, no new runtime dep. Needs env **`SME_STRIPE_API_KEY`**
  (`sk_test_…` in dev); non-2xx raises loudly. ponytail: no Idempotency-Key/retry until live mode.
- `modules/setup/stripe_setup.py` — `@handler("stripe_setup")` (S2.3): creates the Stripe
  product+price and folds `stripe_price_id` onto the product. `cc_sub` only, idempotent, interval
  must be `month`/`year`. Triggered by `POST /api/private/setup/{product_id}/stripe` (202; 404
  missing, 409 unless `setup_ready`, 400 for non-`cc_sub` / no price / bad interval).
- `api/public/funnel.py` — `POST /api/funnel/{slug}/checkout` (S2.3) starts a subscription Checkout
  session passing `client_reference_id` + `metadata[first_touch_token]` for the S2.5 attribution
  join; requires an attribution token (422 without), 409 until Stripe is configured, returns `{url}`.
  The products PATCH keeps the invariant *`stripe_price_id` set ⟹ priced `cc_sub`*: a real price
  change or a switch off `cc_sub` clears it (no-op resubmits preserved). Real-API tests gated on
  `SME_STRIPE_API_KEY`.
- `models/metric_event.py` (S2.5) — `MetricEvent` (TECH_SPEC §4): `product_id`, nullable
  `channel_id`/`content_item_id` (`content_item` arrived in S4.2), `stage`
  (`impression|visit|signup|paid`), `value` (cents for `paid`), `occurred_at`, and a `source`
  provenance/idempotency key (`unique`).
- `api/public/stripe.py` (S2.5) — closes the attribution chain: on a signature-verified
  `checkout.session.completed`, joins `client_reference_id` → lead `funnel_event` → `product_id`
  (fallback: checkout `metadata.product_id`) and writes `metric_event(stage=paid, value=amount_total)`.
  Unattributable session → ack (200), no write. Idempotent on `source="stripe:<session_id>"`
  (app pre-check + a `unique` constraint backstop so a concurrent redelivery can't double-count).
- `api/private/qa.py` — the QA gate. `POST .../qa/{id}/smoke-test` (S2.7) records the pre-QA smoke
  verdict; `POST .../qa/{id}/launch-checklist` (S2.8) emits the deterministic launch checklist from
  real setup state and crosses `setup_done → qa`. At the gate: `POST .../qa/{id}/checklist` (S3.1)
  enqueues an Opus call that generates the click-through `qa_checklist_item` rows (202), `GET` lists
  them. **S3.2:** `PATCH .../qa/{id}/checklist/{item_id}` records a tester's `pass`/`fail` + comment
  (gated to `qa`), and `POST .../qa/{id}/go-live` crosses `qa → live` only when the checklist exists
  and every *blocking* item is `pass` (409 listing the offending ords otherwise; non-blocking fails
  never block).

### Scheduled crank (S4.1)

- `modules/crank/crank.py` — `enqueue_due_cranks`: one `crank` job per LIVE product whose cadence
  has elapsed since its last crank (due-ness is DB-side; no Python tz arithmetic on SQLite
  datetimes). `@handler("crank")` fans out one `generate` child `job_run` per enabled, autonomous,
  non-paused channel × applicable content type (`blog` for `ChannelType.BLOG`, `social` for
  `ChannelType.REDDIT`, `video` for `ChannelType.YOUTUBE` since S5.1). Children carry
  `channel_id`/`content_type` for per-cell crash isolation;
  added (not committed) here — the worker commits them atomically with the crank's DONE status.

### Content generators — social + SEO blog (S4.2 + S4.3)

- `models/content_item.py` — `ContentItem` + `ContentItemStatus`: one row per generated piece,
  scoped by `(product_id, channel_id, content_type)`. Full pipeline state set (`generated →
  critic_passed/failed → guard_failed → scheduled → published → retracted`); nullable seam columns
  (`critic_*`, `idempotency_key`, `scheduled_for`, `published_at`, `external_url`, `error`) are
  pre-seeded so S4.3–S4.7 need no `ALTER TABLE` (no Alembic in v1). The one post-hoc column,
  `spot_check` (S4.9), is added to existing DBs by `db._backfill_additive_columns` (a guarded,
  idempotent `ADD COLUMN` in `init_db`) rather than Alembic.
- `modules/crank/generate.py` (S4.9 spot-check) — on persist, flags the channel's **first** item
  plus a random **10%** (`SPOT_CHECK_RATE`) with `spot_check=true` for async review. The flag is set
  once at creation, orthogonal to `status`, so it never blocks publishing. Surfaced by
  `GET /api/private/content/{id}/spot-check` (newest first) and the dashboard **Spot-check queue**.
- `modules/crank/generate.py` — `@handler("generate")`: budget-gated generate → critic+safety gate
  loop (TECH_SPEC §8.2). Loads product + brief + brand kit; fetches recent items for novelty; calls
  `generate_social_post` or `generate_blog_article`; validates the pillar; then calls
  `critique_content` (haiku tier, a different tier than the generator) to score quality 0–1 and
  decide safety — one call, not two passes. A safety failure hard-blocks (`guard_failed`); a score ≥
  `critic_score_threshold` (default 0.7) accepts (`critic_passed`); a low score regenerates up to
  `critic_max_regenerations` times (default 2), then skips+logs the last candidate
  (`critic_failed`). Exactly one `ContentItem` is persisted per cell with its final status, critic
  score, and critic notes. Injected `generate=`/`critique=` fns keep the handler testable without a
  network call. No commit here — the worker commits atomically with the job's DONE status + summed
  cost.
- `modules/crank/guard.py` — the S4.4 **deterministic guard**, a non-LLM safety net run on the
  critic-approved candidate before it can reach `critic_passed` (independent of the same-family
  critic). `check_content(title, body, brief, product)` returns a failure reason (logged to
  `content_item.error`) or `None`: a blocklist/regex check (`SME_GUARD_BLOCKLIST`, curated default of
  guarantee/compliance red-flags) plus a **numeric claim-trace** requiring each `%`/`Nx`/`$`/large-
  count claim to map to a *same-kind* fact in the brief/product (a `$99` price can't vouch for a
  `99%` claim). A hit hard-blocks (`guard_failed`, no regeneration). Numeric-only by design — a
  paranoid net that errs toward block+log for the async spot-check, not toward publishing.
- `ai/client.py` additions — `SocialPost`, `BlogArticle`, and `CriticVerdict` structured-output
  models; `generate_social_post` / `generate_blog_article` using `claude-opus-4-8` (`GEN_MODEL`)
  with adaptive thinking and a novelty block; `critique_content` using `claude-haiku-4-5`
  (`CRITIC_MODEL`) — a lighter, independent tier so the reviewer doesn't share the writer's blind
  spots. Token caps: `GEN_SOCIAL_MAX_TOKENS=1000`, `GEN_BLOG_MAX_TOKENS=4000`,
  `CRITIC_MAX_TOKENS=600`.

### Attributed funnel + revenue rollup (S6.1)

- `modules/metrics/utm.py` — `thread_utm_links` rewrites marketing-domain links in a published
  item's body to carry `utm_source=<channel type>`, `utm_medium=<content_type>`,
  `utm_campaign=<product slug>`, `utm_content=sme-<content_item_id>` (called from
  `modules/crank/publish.py` right before `adapter.publish`); `resolve_attribution` is the one
  join — shared by the Stripe webhook and the rollup below — that turns a funnel event's UTM
  fields back into `(channel_id, content_item_id)`.
- `api/public/stripe.py` — the S2.5 webhook join now also calls `resolve_attribution` off the
  matched lead's UTM fields, so a `paid` `metric_event` carries `channel_id`/`content_item_id`
  whenever the lead resolved (not just `product_id`).
- `modules/metrics/funnel.py` + `api/private/metrics.py` — `GET
  /api/private/metrics/{product_id}/funnel` (404 unknown product) returns stage totals
  (`impressions`/`visits`/`signups`/`paid`) + `revenue_cents` plus per-`(channel, content_item)`
  attribution rows, sorted by revenue then impressions; unattributed events roll into one
  trailing row. Portfolio (multi-product) roll-up is deferred (TECH_SPEC §14).
- `dashboard/app/products/[id]/funnel.tsx` — renders the rollup as the product page's **Funnel**
  section.

### Content calendar (S6.3)

- `api/private/content.py` — `GET /api/private/content/{product_id}/calendar` returns every
  `ContentItem` regardless of status, newest-first by `COALESCE(published_at, scheduled_for,
  created_at)`, each carrying its own attributed funnel metrics (zeros when nothing's attributed
  yet).
- `modules/metrics/funnel.py` — `metrics_by_content_item` sums `funnel_rollup`'s attribution rows
  by `content_item_id` (channel-only and unattributed rows have no item to land on and are
  dropped), so the calendar reuses the funnel's join instead of re-deriving attribution;
  `zero_metrics` (renamed from `_empty_row_values`) is the shared zeroed shape for both callers.
- `dashboard/lib/calendar.ts` + `app/products/[id]/calendar-grid.tsx` — a pure, unit-tested
  month-grid bucketer (`anchorDate` picks `published_at ?? scheduled_for ?? created_at`; UTC-only
  date math so offset-less API timestamps never drift a day) rendered as a client-side calendar
  with prev/next month paging, status badges, spot-check markers (S4.9's `spot_check` flag), and
  compact per-item performance. `app/products/[id]/content-calendar.tsx` fetches and wires it into
  the product page, degrading to an empty grid on fetch failure (same convention as **Funnel**).

### Phase B media infra (S5.0)

- `celery_app.py` — Celery app for the dedicated `media` queue only (long GPU video/podcast
  jobs, S5.1/S5.2); the text/blog crank stays on the in-process worker loop. Broker/backend is
  Redis (**`SME_CELERY_BROKER_URL`**); `task_routes` sends every `media.*` task to the `media`
  queue; `acks_late` + prefetch 1 so a pod killed mid-job re-delivers instead of dropping.
- `modules/media/tasks.py` — `media.probe` is the only v1 task, proving the enqueue → broker →
  GPU-worker round-trip; real media tasks land here with the same `media.` prefix.
- `modules/media/queue.py` — broker-side introspection (`media_queue_depth`, `media_worker_online`,
  `media_worker_busy`) the orchestrator ticks off of.
- `modules/media/provisioner.py` — `GpuProvisioner` protocol with one implementation (RunPod REST).
  `build_provider()` reads **`SME_GPU_API_KEY`** (§9, never in the DB) and **`SME_GPU_POD_TEMPLATE_ID`**
  (the registered `infra/gpu-worker` image). One provider, 0↔1 pods, adoption-by-name — no
  multi-provider failover (issue #28 non-goal).
- `modules/media/orchestrator.py` — `run_provisioner_tick` (APScheduler job in `scheduler.py`,
  every **`SME_MEDIA_PROVISIONER_INTERVAL_SECONDS`**, default 60): boots a pod when `media` jobs
  are pending and none is running (unless the monthly spend cap says no), tears it down after
  **`SME_GPU_IDLE_TEARDOWN_MINUTES`** (default 10) idle, and reconciles a provider-side loss
  (spot reclaim) into a closed `gpu_lease` row. Never raises — a provider outage alerts (§8.4)
  instead of killing the scheduler.
- `models/gpu_lease.py` — `GpuLease`: one row per pod rental (`active/ended/teardown_unverified/lost`),
  the ledger **`SME_MEDIA_GPU_MONTHLY_CAP_CENTS`** (0=unlimited) sums over at
  **`SME_GPU_POD_RATE_CENTS_PER_MINUTE`** to refuse provisioning past budget.
- Local dev: `infra/compose.dev.yml` runs postgres/redis/flower on non-default loopback ports
  (`docker compose -f infra/compose.dev.yml up -d`) — see `infra/deploy/PORTS.md` for the port
  map and `infra/POSTGRES_MIGRATION.md` for the SQLite→Postgres cutover procedure (not required
  in v1; SQLite/WAL stays primary until Celery worker count forces the swap). The GPU worker
  image is built from `infra/gpu-worker/` (its README covers build/register/broker-transport) and
  runs at the provider, not in this compose stack.

v1 ports (verified free on the dev VPS): FastAPI `:8010`, dashboard `:3010`, Flower `:5555` — see
`infra/deploy/PORTS.md`; run `infra/deploy/check-ports.sh` on the host before binding.
Celery/Redis/Postgres are Phase B additions (S5.0), scoped to the `media` queue above — the
text/blog crank and both APIs still run on the in-process worker loop + SQLite by default.

### Short-form video pipeline (S5.1)

- `modules/crank/generate_video.py` — `run_generate_video` (the video cell of the `generate`
  fan-out, `content_type=video`): LLM script (`generate_video_script`, structured `VideoScript` of
  title/pillar/description/segments) → the same S4.3 critic+safety call and S4.4 deterministic
  guard the text pipeline uses (run on the script text: description + every caption/narration
  line) → on a pass, an ElevenLabs TTS call narrates the full script in one request (needs env
  **`SME_ELEVENLABS_API_KEY`**; voice via `SME_ELEVENLABS_VOICE_ID`, a `SecretStr`, registered with
  the vault's log redaction). The script (with its critic verdict) and the narration MP3 are
  checkpointed under `workspace/{slug}/media/video/job-{id}/` the moment they exist, so a worker
  retry after a crash re-spends no LLM call or TTS request. A gate failure persists the same
  terminal statuses as text (`critic_failed`/`guard_failed`); a pass persists the `ContentItem` at
  `rendering` — this handler never touches the GPU/Celery boundary itself.
- `modules/crank/video_pipeline.py` — `advance_video_renders`, a new scheduler tick (**`video_render`**,
  every `SME_VIDEO_RENDER_TICK_SECONDS`, default 60) that owns the bridge between the two Phase B
  execution planes: dispatches each `rendering` item's checkpointed script+narration as a
  `media.render_video` Celery task (parks on the broker until the S5.0 provisioner boots a pod),
  polls outstanding tasks, and on success writes the returned MP4 into the workspace as
  `item.media_ref` + promotes the item to `critic_passed` (handing it to the existing S4.5
  pace/publish machinery). A failed/oversized result is re-dispatched up to
  `SME_VIDEO_MAX_RENDER_DISPATCHES` (default 3) before the item terminates `render_failed` instead
  of stranding in `rendering` forever; each item commits independently (crash isolation, same
  convention as `publish_scheduled`), and the tick itself never raises.
- `modules/media/video.py` + `modules/media/tasks.py` — `media.render_video`, the second real task
  on the GPU `media` queue (after S5.0's `media.probe`): a **pure** ffmpeg composition (no DB, no
  broker, no app settings) that burns each caption over an equal slice of the narration's duration
  on a solid background and muxes in the audio, returned base64-encoded (Celery's JSON serializer
  can't carry raw bytes) and capped by `SME_VIDEO_RENDER_MAX_BYTES` (default 50 MiB). `ffprobe`/`ffmpeg`
  subprocess calls are wall-clock-bounded (60s/600s) so a hung encode can't wedge the
  `--concurrency=1` pod into looking busy forever. `infra/gpu-worker/Dockerfile` now installs
  `fonts-dejavu-core` for `drawtext`.
- `channels/youtube.py` — `YouTubeAdapter`, the first live channel adapter beyond blog/Reddit:
  uploads `item.media_ref` via the YouTube Data API v3 resumable-upload protocol (init POST for a
  session `Location`, then a byte PUT). Idempotency (no native provider key) embeds
  `sme-ref:{idempotency_key}` in the description and scans the channel's **uploads playlist**
  (near-real-time, unlike the lagging search index) for that marker before uploading, returning the
  existing watch URL instead of re-posting. Errors mirror `reddit.py`'s split: 5xx/429/transport →
  `Retryable`; 401 → `AuthFailure` (fences the channel, S4.8); other 4xx → permanent
  `publish_failed`. `delete` retracts by video id (S4.7), 404 treated as already-gone.
- `modules/crank/oauth_refresh.py` — `OWNED_TOKEN_PROVIDERS` gets its first live entry,
  `ChannelType.YOUTUBE` (Google OAuth, `youtube.upload`/`youtube.readonly` scopes). `OAuthProvider`
  gains `authorize_params` (provider-specific authorize-time query params merged first so core
  protocol params always win) — Google needs `access_type=offline&prompt=consent` or it never
  returns a refresh token.
- `models/content_item.py` — two new terminal-ish statuses, `rendering` (gates passed, GPU render in
  flight) and `render_failed` (re-dispatch budget exhausted); a nullable `media_ref` column
  (workspace-relative path to the rendered artifact, read by the publish adapter). `db.py`'s
  `_backfill_additive_columns` adds `media_ref` to pre-existing SQLite databases the same way S4.9
  added `spot_check` — no Alembic in v1.
- `modules/crank/crank.py` — `ChannelType.YOUTUBE` now maps to `ContentType.VIDEO` in the fan-out
  table, and `AUTONOMOUS_TYPES` (`models/channel.py`) includes `YOUTUBE` alongside blog/Reddit.
