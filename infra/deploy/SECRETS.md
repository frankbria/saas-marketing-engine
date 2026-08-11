# Secrets runbook — what to create, where, and in what order

Companion to [RUNBOOK.md](RUNBOOK.md), which covers *deploying*. This covers the credentials that
deploy needs, and the ones a product needs once it is live. Written for step 3 of the first
deploy ("fill in the secrets") and for the S6.4 acceptance run ([#34](https://github.com/frankbria/saas-marketing-engine/issues/34)).

Two places hold secrets, and they are not interchangeable:

| | Where | Who writes it | Scope |
|---|---|---|---|
| **Platform secrets** | `/etc/sme/sme.env` on the host, mode `600` | You, by hand, once | The whole engine |
| **Channel credentials** | `credential` table, Fernet-encrypted | The dashboard's connect flow | One channel of one product |

Never put a channel credential in `sme.env`. The vault exists so per-channel tokens are encrypted
at rest and scrubbed from logs; pasting one into the env file bypasses both.

---

## Order matters

`SME_VAULT_KEY` first, before anything else, and **never rotate it afterwards**. Every stored
credential is encrypted with it — change it and every connected channel becomes undecryptable with
no error until the next publish fails. Everything below can be added later; this one cannot be
changed later.

---

## 1. `SME_VAULT_KEY` — required, generate first

A Fernet key. Generate on any machine:

```bash
uv run --with cryptography python -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Paste into `sme.env`. Back it up somewhere you will still have in a year — losing it means
re-connecting every channel from scratch.

> There is an in-repo helper for the same thing: `app.secrets.vault.generate_key()`.

## 2. `SME_ANTHROPIC_API_KEY` — required for anything to happen

<https://console.anthropic.com> → **API keys** → Create key.

Every generation step calls it: strategy brief, brand kit, pricing, site copy, QA checklist, and
each piece of content. Without it the engine deploys and serves, and every crank fails.

**Set a spend limit on the key before a two-week unattended run.** The crank generates on a
cadence with no budget ceiling of its own.

## 3. Stripe — required for DoD-2 (attributed revenue)

<https://dashboard.stripe.com> → **Developers**.

- `SME_STRIPE_API_KEY` — API keys → secret key. `sk_test_…` to rehearse, `sk_live_…` for the real
  run. The engine creates the Product and recurring Price itself (`stripe_setup.py`); you do not
  create them by hand.
- `SME_STRIPE_WEBHOOK_SECRET` — Webhooks → add endpoint → `https://<your-public-api>/api/stripe/webhook`,
  subscribe to **`checkout.session.completed`**, then copy the signing secret (`whsec_…`).

The webhook is what closes the attribution loop — UTM → lead → Stripe `client_reference_id` →
`paid` metric. Skip it and the funnel shows visits and signups but never revenue, and DoD-2 cannot
be satisfied.

> The endpoint must be the **public** origin nginx serves (`SME_PUBLIC_API_BASE_URL`), not the
> loopback port. Stripe has to reach it from the internet.

## 4. SMTP — required for DoD-2 (heartbeat confirmation)

Any provider that speaks SMTP. Set `SME_SMTP_HOST`, `SME_SMTP_USER`, `SME_SMTP_PASSWORD`,
`SME_SMTP_FROM`, and `SME_ALERT_EMAIL_TO`.

With `SME_SMTP_HOST` unset, email degrades gracefully: lead capture still works, the send is
skipped and logged. That is fine in dev and **not** fine for the acceptance run — DoD-2 says reach
is *"confirmed by heartbeat"*, and the heartbeat digest and its alerts (`zero_reach`,
`oauth_token_dead`, `repeated_publish_fail`) are delivered by email. Without SMTP the run is
unobserved.

If your provider requires an app-specific password (Gmail, Fastmail, Proton Bridge), generate that
rather than using the account password.

## 5. Reddit — a channel credential, not an env var

This one is **not** in `sme.env`. Reddit is self-managed (`SELF_MANAGED_TYPES`): PRAW refreshes its
own access token, so the vault stores a four-field blob rather than a bearer token.

**The engine does not run Reddit's OAuth flow for you.** The dashboard's authorize/callback
buttons only serve *owned-token* providers — `OWNED_TOKEN_PROVIDERS` currently holds YouTube alone,
and `/authorize` returns 400 for Reddit ("has no redirect-based OAuth provider registered"). You
obtain the refresh token **outside** the engine and hand all four fields over at once.

**1. Create the app:** <https://www.reddit.com/prefs/apps> → *create another app…*

Choose **web app** (not *script*). A script app authenticates by password grant and never issues a
refresh token, which is the one field you cannot do without here. Set the redirect URI to
`http://localhost:8080` — it only has to match what your local token script uses; Reddit never
calls back into the engine.

**2. Get the refresh token** with PRAW's documented one-off flow (*Working with Refresh Tokens*):
a short local script prints an authorize URL, you approve it in a browser, and it exchanges the
code for a refresh token. Request the `identity submit read` scopes and **`duration=permanent`** —
a temporary grant yields a token that silently expires mid-run.

**3. Connect it** — `POST /api/private/channels/{product_id}/{channel_id}/connect` with a `reddit`
block, or the dashboard's connect form:

| Field | Where from |
|---|---|
| `client_id` | under the app name on the prefs page |
| `client_secret` | on the same page |
| `refresh_token` | step 2 |
| `user_agent` | you choose it, e.g. `sme:auto-author:v1 (by /u/<your-handle>)` |

All four are required and rejected if blank — a blank one would mark the channel `connected` and
then fail at publish. The blob is stored encrypted under `reddit_oauth`; PRAW refreshes the access
token itself, so the S4.8 proactive-refresh pass deliberately skips it.

Reddit rate-limits generic user agents. Use a real handle.

> `SME_OAUTH_REDIRECT_BASE_URL` is **not** used by Reddit. It matters for YouTube, whose callback
> is `<base>/api/private/channels/{product_id}/{channel_id}/callback` — per channel, not a single
> global path.

## 6. Optional — only if you want video or podcast channels

Not needed for a blog + Reddit run, which is what DoD asks for.

- `SME_ELEVENLABS_API_KEY` — TTS for video and podcast narration. Unset ⇒ those pipelines fail
  loudly rather than shipping silent audio.
- `SME_GPU_API_KEY`, `SME_GPU_POD_TEMPLATE_ID` — RunPod, for video render and the optional
  ACE-Step music bed. A narration-only episode needs no GPU at all.

If you set the GPU keys, also consider `SME_MEDIA_GPU_MONTHLY_CAP_CENTS` — it defaults to `0`,
which means **no cap**.

---

## Locking it down

```bash
sudo install -m 600 -o root -g root sme.env /etc/sme/sme.env
sudo systemctl restart sme-api sme-dashboard
```

Both systemd units read it via `EnvironmentFile`, and `deploy.sh` sources it. `600` matters: this
is a multi-tenant box (see [PORTS.md](PORTS.md)) and the file holds the vault key.

**Never commit the filled-in copy.** `sme.env.example` is the committed template; the real file
lives only on the host. GitGuardian scans this repo on every PR, but the cheaper control is not
putting it in a working tree at all.

---

## Verifying without printing anything

```bash
# names only, never values
sudo grep -oE '^SME_[A-Z_]+' /etc/sme/sme.env

# which ones are still blank
sudo grep -E '^SME_[A-Z_]+=$' /etc/sme/sme.env

# the engine's own end-to-end check
cd /opt/sme/current && ./infra/deploy/verify-deploy.sh
```

The app registers every vault value with a log-record factory that scrubs it from all log output
(`install_redaction`), and `tests/test_no_plaintext_logging.py` is the static half of that. So
logs are safe by construction — `cat`-ing the env file to a terminal is not.

---

## Pre-flight for the acceptance run

Before starting the ≥2-week clock:

- [ ] `SME_VAULT_KEY` generated **and backed up**
- [ ] `SME_ANTHROPIC_API_KEY` set, with a spend limit
- [ ] Stripe key + webhook secret set, endpoint registered on the **public** origin
- [ ] SMTP set and a test digest actually received — an unobserved run proves nothing
- [ ] Reddit connected **from day one**. `zero_reach` only fires for channels with a platform
      counter, so a blog-only run cannot produce the "non-zero reach confirmed" evidence DoD-2
      needs. Adding Reddit midway restarts that clock.
- [ ] `autoauthor.app` DNS pointed at the host, TLS enabled (`enable-tls.sh`)
- [ ] `SME_PUBLIC_API_BASE_URL` is the real `https://` public origin — Stripe has to reach the
      webhook, and it is baked into every generated landing site's funnel JS
- [ ] `SME_OAUTH_REDIRECT_BASE_URL` is `https://` — `config.py` fails at **startup** on a non-https
      value off localhost, so a wrong value here is a service that will not boot, not a late error
      (irrelevant to Reddit; needed if you ever connect YouTube)
