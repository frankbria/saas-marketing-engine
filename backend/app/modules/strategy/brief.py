"""Strategy-brief job handler (TECH_SPEC §5 / story S1.1).

Wires the S0.2 worker loop to: budget pre-check → ingest → per-file summarize → synthesize →
persist the 1:1 `strategy_brief` row → advance the product to the `strategy` lifecycle state.
The handler returns its token cost in cents; the worker adds it to `job_run.token_cost_cents`.

The LLM/ingest work is injected (`generate`) so the worker wiring + persistence are testable
without a network call; the registered handler passes the real implementation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, func, select

from app.ai.client import (
    SYNTHESIS_MAX_TOKENS,
    SYNTHESIS_MODEL,
    BriefDraft,
    build_client,
    summarize_file,
    synthesize_brief,
)
from app.ai.pricing import cost_cents
from app.config import settings
from app.models import JobRun, LifecycleState, Product, StrategyBrief
from app.modules.strategy.ingest import collect_signal_files, resolve_repo
from app.worker import handler

# generate(product, session, remaining_cents) -> (brief, cost_cents, raw_ai_output)
# remaining_cents is the month's unspent budget (None = unlimited); generate must stop before
# the expensive synthesis call once the run's accumulated cost reaches it.
GenerateFn = Callable[[Product, Session, int | None], tuple[BriefDraft, int, str]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def month_to_date_cost_cents(session: Session, product_id: int, now: datetime) -> int:
    """Sum of token cost across this product's job_runs since the start of the current UTC month."""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = session.exec(
        select(func.coalesce(func.sum(JobRun.token_cost_cents), 0)).where(
            JobRun.product_id == product_id, JobRun.created_at >= month_start
        )
    ).one()
    return int(total)


def _real_generate(
    product: Product, _session: Session, remaining_cents: int | None
) -> tuple[BriefDraft, int, str]:
    client = build_client()
    dest = Path(settings.workspace_root) / product.slug / "repo"
    repo = resolve_repo(product.repo_local_path, product.repo_url, dest)

    files = collect_signal_files(repo)
    if not files:
        raise RuntimeError(f"no signal files found in repo for product {product.id}")

    def _over_budget(spent: int) -> bool:
        return remaining_cents is not None and spent >= remaining_cents

    cost = 0
    summaries: list[tuple[str, str]] = []
    for relpath, content in files:
        # Stop the (bounded, cheap) summary loop the moment the run reaches the cap, so we never
        # then make the expensive synthesis call. Worst-case overshoot is one haiku summary.
        if _over_budget(cost):
            raise RuntimeError(f"token budget exhausted mid-ingest for product {product.id}")
        summary, c = summarize_file(client, relpath, content)
        summaries.append((relpath, summary))
        cost += c

    # Reserve a conservative upper bound for the synthesis call before making it, so a small
    # remaining budget can't be blown past by the (most expensive) Opus call. ~3 chars/token is a
    # deliberately low estimate → higher token count → higher reserve → we err toward refusing.
    if remaining_cents is not None:
        est_input_tokens = sum(len(s) for _, s in summaries) // 3
        reserve = cost_cents(SYNTHESIS_MODEL, est_input_tokens, SYNTHESIS_MAX_TOKENS)
        if cost + reserve > remaining_cents:
            raise RuntimeError(
                f"insufficient budget to reserve for synthesis for product {product.id} "
                f"(need ~{cost + reserve}, have {remaining_cents} cents)"
            )

    brief, c = synthesize_brief(client, product.name, product.description, summaries)
    cost += c
    return brief, cost, brief.model_dump_json()


def generate_strategy_brief(
    job: JobRun, session: Session, *, generate: GenerateFn = _real_generate
) -> int:
    """Produce + persist the brief for `job.product_id`. Returns token cost in cents."""
    if job.product_id is None:
        raise LookupError("strategy_brief job has no product_id")
    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")

    # Budget gate: 0 means unset/unlimited (onboarding default). The pre-check blocks a run that's
    # already over; `remaining` is handed to generate so it stops before the costly synthesis call
    # once this run's spend reaches the cap (bounding overshoot to the cheap summary phase).
    budget = product.token_budget_cents_month
    remaining: int | None = None
    if budget > 0:
        spent = month_to_date_cost_cents(session, product.id, _utcnow())
        if spent >= budget:
            raise RuntimeError(
                f"product {product.id} over monthly token budget ({spent} >= {budget} cents)"
            )
        remaining = budget - spent

    brief, cost, raw = generate(product, session, remaining)
    _upsert_brief(session, product.id, brief, raw)

    product.lifecycle_state = LifecycleState.STRATEGY
    product.updated_at = _utcnow()
    session.add(product)
    # No commit here: leave the brief + product changes pending so the worker's final commit
    # persists them atomically with the job's DONE status and token_cost_cents. Committing here
    # would let a crash before that commit requeue the job and double-spend (reclaim_running_jobs).
    return cost


def _upsert_brief(session: Session, product_id: int, brief: BriefDraft, raw: str) -> StrategyBrief:
    row = session.exec(select(StrategyBrief).where(StrategyBrief.product_id == product_id)).first()
    fields = {
        "icp_json": brief.icp.model_dump_json(),
        "pain_points_json": json.dumps(brief.pain_points),
        "positioning": brief.positioning,
        "channel_plan_json": json.dumps([c.model_dump() for c in brief.channel_plan]),
        "content_pillars_json": json.dumps(brief.content_pillars),
        "cadence_json": brief.cadence.model_dump_json(),
        "raw_ai_output": raw,
    }
    if row is None:
        row = StrategyBrief(product_id=product_id, **fields)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = _utcnow()
    session.add(row)
    return row


# Indirection so tests can drive the full enqueue → run_due_jobs path with a stub generator
# (no network), while production uses the real ingest + LLM implementation.
_GENERATE: GenerateFn = _real_generate


@handler("strategy_brief")
def _strategy_brief_handler(job: JobRun, session: Session) -> int:
    return generate_strategy_brief(job, session, generate=_GENERATE)
