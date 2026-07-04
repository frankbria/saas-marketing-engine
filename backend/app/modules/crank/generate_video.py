"""Video generate handler: script → gates → TTS → dispatch to the GPU render queue (S5.1, #29).

The video cell of the crank fan-out runs on the same worker seam as text (`@handler("generate")`
routes here for `content_type=video`), but the pipeline splits across the two Phase B execution
planes (TECH_SPEC §8/§10): every CPU/API step — LLM script, critic+safety gate (S4.3),
deterministic guard (S4.4), ElevenLabs TTS — runs in-process on the VPS; only the render rides the
Celery `media` queue to the ephemeral GPU worker (S5.0). This handler ends at `status=rendering`
with the render inputs checkpointed in the workspace; the `video_pipeline` tick owns dispatching
to and collecting from the queue, so a long render never blocks the in-process worker loop.

Gates run on the *script text* before TTS or any GPU spend — a rejected script costs one LLM pass,
not a pod boot. Resumability (AC): the script (with its critic verdict) and the narration audio are
checkpointed under `workspace/{slug}/media/video/job-{id}/` the moment they exist, and each step
skips itself when its artifact is already on disk — a worker retry after a crash re-spends nothing,
and re-running the handler can never double-create the row (the worker's commit is atomic with the
job's DONE status, so a crashed run leaves only files, never a row).
"""

from __future__ import annotations

import json
import os
import random
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlmodel import Session, select

from app.ai.client import (
    CRITIC_MAX_TOKENS,
    CRITIC_MODEL,
    GEN_MODEL,
    GEN_VIDEO_MAX_TOKENS,
    BrandKit,
    CriticVerdict,
    VideoScript,
    build_client,
    critique_content,
    generate_video_script,
)
from app.ai.pricing import cost_cents
from app.config import settings
from app.models import ContentItem, Product, StrategyBrief
from app.models.content_item import ContentItemStatus
from app.modules.crank.crank import ContentType
from app.modules.crank.generate import (
    SPOT_CHECK_RATE,
    CritiqueFn,
    Generated,
    _is_first_for_channel,
    _recent_items,
    _require_known_pillar,
    _reservation_input_estimate,
)
from app.modules.crank.guard import check_content
from app.modules.strategy.brief import month_to_date_cost_cents
from app.secrets.vault import register_secret

# generate(product, brief, brand_kit, recent_items) -> (VideoScript, cost_cents)
VideoGenerateFn = Callable[[Product, StrategyBrief, BrandKit, list[str]], tuple[VideoScript, int]]
# tts(script) -> narration audio bytes (mp3). Provider-billed, not token-billed → no cost_cents.
TtsFn = Callable[[VideoScript], bytes]

SCRIPT_FILE = "script.json"
NARRATION_FILE = "narration.mp3"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _real_generate(
    product: Product, brief: StrategyBrief, brand_kit: BrandKit, recent_items: list[str]
) -> tuple[VideoScript, int]:
    return generate_video_script(
        build_client(),
        product.name,
        brand_kit,
        brief.positioning,
        json.loads(brief.content_pillars_json),
        recent_items,
    )


def _real_critique(
    product: Product, brand_kit: BrandKit, content_type: str, candidate: Generated
) -> tuple[CriticVerdict, int]:
    return critique_content(
        build_client(), product.name, brand_kit, content_type, candidate.title, candidate.body
    )


def _real_tts(script: VideoScript) -> bytes:
    """Narrate the full script in one ElevenLabs call (CPU/API step — VPS, never the GPU queue).
    Raises on any failure so the worker's retry loop re-runs it; the narration checkpoint makes
    the retry free for the steps before it."""
    key = settings.elevenlabs_api_key
    if key is None:
        raise RuntimeError("SME_ELEVENLABS_API_KEY is not set; cannot narrate the video script")
    register_secret(key.get_secret_value())  # §9: the key must never appear in a log line
    narration = " ".join(seg.narration for seg in script.segments)
    response = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}",
        headers={"xi-api-key": key.get_secret_value()},
        json={"text": narration, "model_id": "eleven_multilingual_v2"},
        timeout=120.0,
    )
    response.raise_for_status()
    return response.content


def _atomic_write(path: Path, data: bytes) -> None:
    """Temp-file + rename so a crash mid-write can never leave a half-artifact a resumed run
    would trust (same idiom as the blog channel's site writes)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _script_text(script: VideoScript) -> str:
    """The reviewable substance persisted as `content_item.body` and vetted by the gates: the
    YouTube description plus every caption + narration line (what viewers see and hear)."""
    lines = [f"[{seg.caption}] {seg.narration}" for seg in script.segments]
    return script.description + "\n\n" + "\n".join(lines)


def _video_dir_rel(product: Product, job_id: int) -> str:
    """Workspace-relative artifact dir; `media_ref` derives from it so the path survives a
    workspace_root move (PRD G7: paths key off the product slug)."""
    return f"{product.slug}/media/video/job-{job_id}"


def _reserve_one_attempt(
    product_name: str, brief: StrategyBrief, brand_json: str, recent_items: list[str]
) -> int:
    """Worst-case cost of one script + critic pass, for the budget gate (mirrors generate.py)."""
    est_input = _reservation_input_estimate(product_name, brief, brand_json, recent_items)
    gen_reserve = cost_cents(GEN_MODEL, est_input, GEN_VIDEO_MAX_TOKENS)
    critic_input = GEN_VIDEO_MAX_TOKENS + len(brand_json) // 3 + 200
    critic_reserve = cost_cents(CRITIC_MODEL, critic_input, CRITIC_MAX_TOKENS)
    return gen_reserve + critic_reserve


def run_generate_video(
    job,
    session: Session,
    *,
    generate: VideoGenerateFn = _real_generate,
    critique: CritiqueFn = _real_critique,
    tts: TtsFn = _real_tts,
    sample: Callable[[], float] = random.random,
) -> int:
    """Script → critic+safety gate → guard → TTS → persist one `rendering` item (S5.1).

    Mirrors `run_generate`'s regeneration/budget contract for the script, then (only on a pass)
    checkpoints the render inputs and persists the item at `rendering` — the video tick dispatches
    the GPU task from the checkpoints. Failure paths persist the same terminal statuses as text
    (`critic_failed` / `guard_failed`). Returns the summed LLM cost in cents."""
    if job.product_id is None or job.channel_id is None or job.content_type is None:
        raise LookupError(
            f"generate job {job.id} missing product_id/channel_id/content_type "
            "(should be set by the crank fan-out)"
        )
    if job.content_type != ContentType.VIDEO.value:
        raise LookupError(
            f"video generate job {job.id} has content_type {job.content_type!r} (expected video)"
        )

    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")
    if product.brand_json is None:
        raise LookupError(f"product {product.id} has no brand_json (brand kit not generated)")
    brief = session.exec(
        select(StrategyBrief).where(StrategyBrief.product_id == product.id)
    ).first()
    if brief is None:
        raise LookupError(f"product {product.id} has no strategy brief")

    brand_kit = BrandKit.model_validate_json(product.brand_json)
    video_dir_rel = _video_dir_rel(product, job.id)
    video_dir = Path(settings.workspace_root) / video_dir_rel
    script_path = video_dir / SCRIPT_FILE

    total_cost = 0
    if script_path.exists():
        # Resume after a worker crash: the checkpoint only ever holds a gate-passed script (it is
        # written below strictly after the gates), so reuse it — and its verdict — without
        # re-spending an LLM call.
        checkpoint = json.loads(script_path.read_text())
        script = VideoScript.model_validate(checkpoint["script"])
        verdict = CriticVerdict.model_validate(checkpoint["critic"])
        status: ContentItemStatus = ContentItemStatus.RENDERING
        guard_error: str | None = None
    else:
        script, verdict, status, guard_error, total_cost = _generate_gated_script(
            job, session, product, brief, brand_kit, generate=generate, critique=critique
        )

    if status is ContentItemStatus.RENDERING:
        # Checkpoint the gate-passed script (with its verdict) and the narration; each write is
        # skipped when the artifact already exists so retries re-spend nothing.
        video_dir.mkdir(parents=True, exist_ok=True)
        if not script_path.exists():
            payload = {"script": script.model_dump(), "critic": verdict.model_dump()}
            _atomic_write(script_path, json.dumps(payload).encode())
        narration_path = video_dir / NARRATION_FILE
        if not narration_path.exists():
            _atomic_write(narration_path, tts(script))

    spot_check = (
        _is_first_for_channel(session, product.id, job.channel_id) or sample() < SPOT_CHECK_RATE
    )
    meta = {
        "pillar": script.pillar,
        "description": script.description,
        "video_dir": video_dir_rel,
        # Dispatch bookkeeping belongs to the video tick (it owns the Celery boundary); the
        # handler only seeds the counter so the tick's re-dispatch bound reads uniformly.
        "render": {"dispatches": 0},
    }
    if status is not ContentItemStatus.RENDERING:
        meta.pop("render")  # a gate-failed item never renders
    session.add(
        ContentItem(
            product_id=product.id,
            channel_id=job.channel_id,
            content_type=job.content_type,
            status=status,
            title=script.title,
            body=_script_text(script),
            meta_json=json.dumps(meta),
            critic_score=verdict.score,
            critic_notes=verdict.notes,
            error=guard_error,
            spot_check=spot_check,
        )
    )
    # No commit here: the worker commits the item atomically with the job's DONE status + cost.
    return total_cost


def _generate_gated_script(
    job,
    session: Session,
    product: Product,
    brief: StrategyBrief,
    brand_kit: BrandKit,
    *,
    generate: VideoGenerateFn,
    critique: CritiqueFn,
) -> tuple[VideoScript, CriticVerdict, ContentItemStatus, str | None, int]:
    """The S4.3/S4.4 gate loop on the script text (budget-reserved per attempt, regeneration on a
    low score, hard block on safety/guard) — same contract as `run_generate`, factored out so the
    checkpoint-resume path above can bypass it wholesale."""
    pillars = json.loads(brief.content_pillars_json)
    recent_items = _recent_items(session, product.id, job.channel_id)

    budget = product.token_budget_cents_month
    remaining: int | None = None
    if budget > 0:
        spent = month_to_date_cost_cents(session, product.id, _utcnow())
        if spent >= budget:
            raise RuntimeError(
                f"product {product.id} over monthly token budget ({spent} >= {budget} cents)"
            )
        remaining = budget - spent
    reserve_per_attempt = _reserve_one_attempt(
        product.name, brief, product.brand_json, recent_items
    )

    total_cost = 0
    script: VideoScript | None = None
    verdict: CriticVerdict | None = None
    status: ContentItemStatus | None = None
    guard_error: str | None = None
    for _attempt in range(1 + settings.critic_max_regenerations):
        if remaining is not None and total_cost + reserve_per_attempt > remaining:
            if script is None:  # can't even afford the first pass — fail loudly (no partial spend)
                raise RuntimeError(
                    f"insufficient budget to reserve a script+critic pass for product "
                    f"{product.id} (need ~{reserve_per_attempt}, have {remaining} cents)"
                )
            break  # can't afford another regeneration → keep the last (low-scoring) candidate
        script, gen_cost = generate(product, brief, brand_kit, recent_items)
        total_cost += gen_cost
        _require_known_pillar(script.pillar, pillars, "video")
        candidate = Generated(title=script.title, body=_script_text(script), meta={})
        verdict, critic_cost = critique(product, brand_kit, job.content_type, candidate)
        total_cost += critic_cost
        if not verdict.safety_pass:  # hard block, no regeneration (S4.3)
            status = ContentItemStatus.GUARD_FAILED
            break
        if verdict.score >= settings.critic_score_threshold:
            # S4.4: deterministic guard on the critic-approved script — captions + narration are
            # exactly what ships on screen/audio, so the same text the critic saw is what's vetted.
            guard_error = check_content(script.title, candidate.body, brief, product)
            status = (
                ContentItemStatus.GUARD_FAILED
                if guard_error is not None
                else ContentItemStatus.RENDERING
            )
            break
        # low score → regenerate if an attempt (and budget) remains

    if status is None:  # exhausted attempts / budget-stopped without passing → skip+log
        status = ContentItemStatus.CRITIC_FAILED

    assert script is not None and verdict is not None  # the no-attempt path raises above
    return script, verdict, status, guard_error, total_cost


# Indirection so tests can drive the full enqueue → run_due_jobs path with stubs (no network),
# while production uses the real LLM/TTS implementations — mirrors generate.py's _GENERATE seam.
_GENERATE: VideoGenerateFn = _real_generate
_CRITIQUE: CritiqueFn = _real_critique
_TTS: TtsFn = _real_tts


def run_generate_video_job(job, session: Session) -> int:
    """Worker-facing wrapper bound to the module seams (called by the `generate` handler)."""
    return run_generate_video(job, session, generate=_GENERATE, critique=_CRITIQUE, tts=_TTS)
