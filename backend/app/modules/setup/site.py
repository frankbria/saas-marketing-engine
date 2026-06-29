"""Landing-site build handler (TECH_SPEC §6.1 / story S2.1).

The engine builds each site from one maintained `site-template/`, injecting AI-written copy slots +
brand tokens (from `product.brand_json`); layout/plumbing stay constant. Mirrors the S1.2 brand
handler: budget pre-check → one structured Opus call for the site content → render → static export
to the product workspace → deploy under `product.marketing_domain`. The handler returns its token
cost; the worker adds it to `job_run.token_cost_cents` and commits atomically.

Render/build/deploy are pure filesystem + templating (no network) so the wiring is testable without
spending tokens; only the AI copy call is injected (`generate`), exactly like brand.py.

ponytail: "deploy" places the static files under the configured nginx web root and emits a vhost —
`nginx -s reload`, TLS/cert issuance, and remote copy are operational steps the live smoke test
(S2.7) and end-to-end DoD (S6.4) exercise.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import Session, select

from app.ai.client import (
    SITE_MAX_TOKENS,
    SITE_MODEL,
    BrandKit,
    SiteContent,
    build_client,
    generate_site_content,
)
from app.ai.pricing import cost_cents
from app.config import settings
from app.models import Product, StrategyBrief
from app.modules.strategy.brief import month_to_date_cost_cents
from app.worker import handler
from app.workspace import workspace_path

# site-template/ lives at the repo root, four parents up from this file
# (setup → modules → app → backend → repo root).
_TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "site-template"
_TEMPLATE_NAME = "index.html.j2"

# marketing_domain becomes a filesystem path component AND an nginx `server_name` — both dangerous
# if it isn't a real hostname. The product API takes it as a free-form string, so reject anything
# that isn't a plain DNS hostname (no path separators, `..`, control chars, or nginx metacharacters)
# at the point of use, closing path-traversal (rmtree/copytree) and config-injection.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

# Autoescape unconditionally — AI/owner copy is injected into a public page, so escaping is an
# XSS guard, not a nicety. `| tojson` in the template handles the JS-string config safely.
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(default=True, default_for_string=True),
)

# generate(product, brand_kit, positioning, remaining_cents) -> (content, cost_cents)
# remaining_cents is the month's unspent budget (None = unlimited); generate must refuse before the
# Opus call if it can't reserve the call's worst-case cost.
GenerateFn = Callable[[Product, BrandKit, str, "int | None"], "tuple[SiteContent, int]"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def render_site(slug: str, content: SiteContent, *, api_base_url: str) -> str:
    """Render the landing page HTML for one product. Pure: template + injected content only."""
    template = _env.get_template(_TEMPLATE_NAME)
    return template.render(slug=slug, content=content, api_base_url=api_base_url)


def build_site(product: Product, content: SiteContent) -> Path:
    """Static export: render + write `index.html` into the workspace. Returns the site dir."""
    html = render_site(product.slug, content, api_base_url=settings.public_api_base_url)
    site_dir = workspace_path(product.slug) / "site"
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "index.html").write_text(html, encoding="utf-8")
    return site_dir


def deploy_site(product: Product, site_dir: Path) -> Path:
    """Place the static site under nginx's web root keyed by `marketing_domain` + emit a vhost."""
    domain = product.marketing_domain
    if not domain:
        raise RuntimeError(f"product {product.id} has no marketing_domain; cannot deploy site")
    if not _HOSTNAME_RE.match(domain):
        raise RuntimeError(
            f"product {product.id} marketing_domain {domain!r} is not a valid hostname; refusing "
            "to use it as a filesystem path / nginx server_name"
        )
    root = Path(settings.nginx_sites_root)
    dest = root / domain
    if dest.exists():
        shutil.rmtree(dest)  # replace wholesale — the build is the source of truth
    shutil.copytree(site_dir, dest)
    # ponytail: HTTP-only vhost; TLS termination + `nginx -s reload` are operational (S2.7/S6.4).
    vhost = (
        f"server {{\n"
        f"    listen 80;\n"
        f"    server_name {domain};\n"
        f"    root {dest};\n"
        f"    index index.html;\n"
        f"    location / {{ try_files $uri $uri/ /index.html; }}\n"
        f"}}\n"
    )
    (root / f"{domain}.conf").write_text(vhost, encoding="utf-8")
    return dest


def _real_generate(
    product: Product, brand_kit: BrandKit, positioning: str, remaining_cents: int | None
) -> tuple[SiteContent, int]:
    client = build_client()

    # Reserve a conservative upper bound before the Opus call so a small remaining budget can't be
    # blown past it (mirrors brand.py). ~3 chars/token under-estimates → higher reserve → err toward
    # refusing.
    if remaining_cents is not None:
        PROMPT_OVERHEAD_CHARS = 700  # fixed system + user template text around the inputs
        input_chars = (
            len(product.name)
            + len(product.description or "")
            + len(positioning)
            + len(product.brand_json or "")
            + PROMPT_OVERHEAD_CHARS
        )
        reserve = cost_cents(SITE_MODEL, input_chars // 3, SITE_MAX_TOKENS)
        if reserve > remaining_cents:
            raise RuntimeError(
                f"insufficient budget to reserve for site content for product {product.id} "
                f"(need ~{reserve}, have {remaining_cents} cents)"
            )

    return generate_site_content(client, product.name, product.description, brand_kit, positioning)


def build_product_site(job, session: Session, *, generate: GenerateFn = _real_generate) -> int:
    """Generate + render + deploy the landing site for `job.product_id`. Returns cost in cents."""
    if job.product_id is None:
        raise LookupError("setup_site job has no product_id")
    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")

    # The brand kit is the site's grounding (S2.1 depends on S1.2). Without it there's nothing to be
    # on-brand for — surface rather than rendering a blank site.
    if not product.brand_json:
        raise RuntimeError(f"product {product.id} has no brand kit; run the brand kit first")
    brand_kit = BrandKit.model_validate_json(product.brand_json)

    # Positioning sharpens the headline; the brief is guaranteed to exist (brand kit needs it) but
    # stay defensive about a missing/blank value.
    brief = session.exec(
        select(StrategyBrief).where(StrategyBrief.product_id == product.id)
    ).first()
    positioning = brief.positioning if brief else ""

    # Budget gate: 0 means unset/unlimited (onboarding default). Pre-check blocks an already-over
    # run; `remaining` lets generate refuse before the costly Opus call.
    budget = product.token_budget_cents_month
    remaining: int | None = None
    if budget > 0:
        spent = month_to_date_cost_cents(session, product.id, _utcnow())
        if spent >= budget:
            raise RuntimeError(
                f"product {product.id} over monthly token budget ({spent} >= {budget} cents)"
            )
        remaining = budget - spent

    content, cost = generate(product, brand_kit, positioning, remaining)
    site_dir = build_site(product, content)
    if product.marketing_domain:
        deploy_site(product, site_dir)
    return cost


# Indirection so tests can drive the full enqueue → run_due_jobs path with a stub generator
# (no network), while production uses the real LLM implementation.
_GENERATE: GenerateFn = _real_generate


@handler("setup_site")
def _setup_site_handler(job, session: Session) -> int:
    return build_product_site(job, session, generate=_GENERATE)
