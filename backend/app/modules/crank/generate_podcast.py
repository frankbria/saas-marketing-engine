"""Podcast generate handler: script → gates → TTS → (finalize | dispatch a music mix) (S5.2, #30).

The podcast cell of the crank fan-out runs on the same worker seam as text/video
(`@handler("generate")` routes here for `content_type=podcast`), and gates the *script text* before
any TTS or GPU spend — a rejected episode costs one LLM pass, not a pod boot (mirrors S5.1).

Where it diverges from video: the expensive external step (ElevenLabs narration) is a CPU/API call
on the VPS, not a GPU render — so the default episode finishes *in-process*. Only an opt-in music
bed (channel `profile_json.music_bed`) crosses to the GPU `media` queue:

- **No music bed (default):** the narration MP3 *is* the episode → `media_ref` + `critic_passed`
  right here, entirely on the VPS. **Never touches the `media` queue → zero GPU minutes** (AC).
- **Music bed:** the gate-passed script (with its verdict) and the narration are checkpointed and
  the item is left `rendering`; the `podcast_pipeline` tick dispatches the ACE-Step mix on the GPU
  pod and collects it back — exactly the S5.1 dispatch/collect shape.

Resumability (AC / §8.3): the script and narration are checkpointed under
`workspace/{slug}/media/podcast/job-{id}/` the moment they exist, and each step skips itself when
its artifact is already on disk — a worker retry after a crash re-spends nothing, and the worker's
commit is atomic with the job's DONE status so a crashed run leaves only files, never a row.
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
    GEN_PODCAST_MAX_TOKENS,
    BrandKit,
    CriticVerdict,
    PodcastScript,
    build_client,
    critique_content,
    generate_podcast_script,
)
from app.ai.pricing import cost_cents
from app.config import settings
from app.models import Channel, ContentItem, Product, StrategyBrief
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

# generate(product, brief, brand_kit, recent_items) -> (PodcastScript, cost_cents)
PodcastGenerateFn = Callable[
    [Product, StrategyBrief, BrandKit, list[str]], tuple[PodcastScript, int]
]
# tts(script) -> narration audio bytes (mp3). Provider-billed, not token-billed → no cost_cents.
TtsFn = Callable[[PodcastScript], bytes]

SCRIPT_FILE = "script.json"
NARRATION_FILE = "narration.mp3"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _real_generate(
    product: Product, brief: StrategyBrief, brand_kit: BrandKit, recent_items: list[str]
) -> tuple[PodcastScript, int]:
    return generate_podcast_script(
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


def _real_tts(script: PodcastScript) -> bytes:
    """Narrate the full episode in one ElevenLabs call (CPU/API step — VPS, never the GPU queue).
    Raises on any failure so the worker's retry loop re-runs it; the narration checkpoint makes the
    retry free for the steps before it."""
    key = settings.elevenlabs_api_key
    if key is None:
        raise RuntimeError("SME_ELEVENLABS_API_KEY is not set; cannot narrate the podcast episode")
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
    """Temp-file + rename so a crash mid-write can never leave a half-artifact a resumed run would
    trust (same idiom as generate_video.py / the blog channel's site writes)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _script_text(script: PodcastScript) -> str:
    """The reviewable substance persisted as `content_item.body` and vetted by the gates: the
    episode description (RSS show notes) plus every section heading + spoken narration line (what
    listeners see in the notes and hear)."""
    lines = [f"[{seg.heading}] {seg.narration}" for seg in script.segments]
    return script.description + "\n\n" + "\n".join(lines)


def _podcast_dir_rel(product: Product, job_id: int) -> str:
    """Workspace-relative artifact dir; `media_ref` derives from it so the path survives a
    workspace_root move (PRD G7: paths key off the product slug)."""
    return f"{product.slug}/media/podcast/job-{job_id}"


def _music_bed_requested(channel: Channel | None) -> bool:
    """Whether this channel opts into an ACE-Step music bed (folded onto `profile_json`, like the
    reddit target). Default off — the zero-GPU narration-only path is the norm."""
    if channel is None or not channel.profile_json:
        return False
    try:
        profile = json.loads(channel.profile_json) or {}
    except json.JSONDecodeError:
        return False  # a malformed profile must not silently enable paid GPU work
    return bool(profile.get("music_bed"))


def _reserve_one_attempt(
    product_name: str, brief: StrategyBrief, brand_json: str, recent_items: list[str]
) -> int:
    """Worst-case cost of one script + critic pass, for the budget gate (mirrors generate.py)."""
    est_input = _reservation_input_estimate(product_name, brief, brand_json, recent_items)
    gen_reserve = cost_cents(GEN_MODEL, est_input, GEN_PODCAST_MAX_TOKENS)
    critic_input = GEN_PODCAST_MAX_TOKENS + len(brand_json) // 3 + 200
    critic_reserve = cost_cents(CRITIC_MODEL, critic_input, CRITIC_MAX_TOKENS)
    return gen_reserve + critic_reserve


def run_generate_podcast(
    job,
    session: Session,
    *,
    generate: PodcastGenerateFn = _real_generate,
    critique: CritiqueFn = _real_critique,
    tts: TtsFn = _real_tts,
    sample: Callable[[], float] = random.random,
) -> int:
    """Script → critic+safety gate → guard → TTS → finalize or dispatch a music mix (S5.2).

    Mirrors `run_generate`'s regeneration/budget contract for the script, then (only on a pass)
    checkpoints the narration and either finalizes the episode in-process (no music bed → the
    narration is the episode, `critic_passed`, zero GPU) or leaves it `rendering` for the podcast
    tick to mix a bed on the GPU pod. Failure paths persist the same terminal statuses as text
    (`critic_failed` / `guard_failed`). Returns the summed LLM cost in cents."""
    if job.product_id is None or job.channel_id is None or job.content_type is None:
        raise LookupError(
            f"generate job {job.id} missing product_id/channel_id/content_type "
            "(should be set by the crank fan-out)"
        )
    if job.content_type != ContentType.PODCAST.value:
        raise LookupError(
            f"podcast generate job {job.id} content_type {job.content_type!r} (expected podcast)"
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

    channel = session.get(Channel, job.channel_id)
    music_bed = _music_bed_requested(channel)
    brand_kit = BrandKit.model_validate_json(product.brand_json)
    podcast_dir_rel = _podcast_dir_rel(product, job.id)
    podcast_dir = Path(settings.workspace_root) / podcast_dir_rel
    script_path = podcast_dir / SCRIPT_FILE

    total_cost = 0
    if script_path.exists():
        # Resume after a worker crash: the checkpoint only ever holds a gate-passed script (written
        # below strictly after the gates), so reuse it — and its verdict — without re-spending an
        # LLM call. The crashed attempt's LLM/TTS spend is not re-recorded here (cost 0), the same
        # crash-window imprecision documented in generate_video.py; bounded by MAX_ATTEMPTS/budget.
        checkpoint = json.loads(script_path.read_text())
        script = PodcastScript.model_validate(checkpoint["script"])
        verdict = CriticVerdict.model_validate(checkpoint["critic"])
        passed = True
        status: ContentItemStatus = ContentItemStatus.GENERATED  # provisional; set on the pass
        guard_error: str | None = None
    else:
        script, verdict, status, guard_error, total_cost = _generate_gated_script(
            job, session, product, brief, brand_kit, generate=generate, critique=critique
        )
        passed = status is ContentItemStatus.CRITIC_PASSED

    media_ref: str | None = None
    if passed:
        # Checkpoint the gate-passed script (with its verdict) and the narration; each write is
        # skipped when the artifact already exists so retries re-spend nothing.
        podcast_dir.mkdir(parents=True, exist_ok=True)
        if not script_path.exists():
            payload = {"script": script.model_dump(), "critic": verdict.model_dump()}
            _atomic_write(script_path, json.dumps(payload).encode())
        narration_path = podcast_dir / NARRATION_FILE
        if not narration_path.exists():
            _atomic_write(narration_path, tts(script))

        if music_bed:
            # The GPU tick mixes an ACE-Step bed under the narration, then promotes to critic_passed
            status = ContentItemStatus.RENDERING
        else:
            # No bed → the narration IS the episode; publish-ready right here (zero GPU minutes).
            status = ContentItemStatus.CRITIC_PASSED
            media_ref = f"{podcast_dir_rel}/{NARRATION_FILE}"

    spot_check = (
        _is_first_for_channel(session, product.id, job.channel_id) or sample() < SPOT_CHECK_RATE
    )
    meta = {
        "pillar": script.pillar,
        "description": script.description,
        "podcast_dir": podcast_dir_rel,
    }
    if status is ContentItemStatus.RENDERING:
        # The music bed the mixer will generate, and the dispatch bookkeeping the podcast tick owns
        # (it owns the Celery boundary; the handler only seeds the counter).
        meta["music_prompt"] = script.music_prompt or f"instrumental bed for {product.name}"
        meta["render"] = {"dispatches": 0}
    session.add(
        ContentItem(
            product_id=product.id,
            channel_id=job.channel_id,
            content_type=job.content_type,
            status=status,
            title=script.title,
            body=_script_text(script),
            meta_json=json.dumps(meta),
            media_ref=media_ref,
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
    generate: PodcastGenerateFn,
    critique: CritiqueFn,
) -> tuple[PodcastScript, CriticVerdict, ContentItemStatus, str | None, int]:
    """The S4.3/S4.4 gate loop on the script text (budget-reserved per attempt, regeneration on a
    low score, hard block on safety/guard) — same contract as `run_generate`. Returns
    `critic_passed` as the "gates passed" signal; the caller remaps it to `rendering` when a music
    bed is requested. Factored out so the checkpoint-resume path bypasses it wholesale."""
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
    script: PodcastScript | None = None
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
        _require_known_pillar(script.pillar, pillars, "podcast")
        candidate = Generated(title=script.title, body=_script_text(script), meta={})
        verdict, critic_cost = critique(product, brand_kit, job.content_type, candidate)
        total_cost += critic_cost
        if not verdict.safety_pass:  # hard block, no regeneration (S4.3)
            status = ContentItemStatus.GUARD_FAILED
            break
        if verdict.score >= settings.critic_score_threshold:
            # S4.4: deterministic guard on the critic-approved script — the show notes + narration
            # are exactly what ships, so the same text the critic saw is what's vetted.
            guard_error = check_content(script.title, candidate.body, brief, product)
            status = (
                ContentItemStatus.GUARD_FAILED
                if guard_error is not None
                else ContentItemStatus.CRITIC_PASSED
            )
            break
        # low score → regenerate if an attempt (and budget) remains

    if status is None:  # exhausted attempts / budget-stopped without passing → skip+log
        status = ContentItemStatus.CRITIC_FAILED

    assert script is not None and verdict is not None  # the no-attempt path raises above
    return script, verdict, status, guard_error, total_cost


# Indirection so tests can drive the full enqueue → run_due_jobs path with stubs (no network),
# while production uses the real LLM/TTS implementations — mirrors generate.py's _GENERATE seam.
_GENERATE: PodcastGenerateFn = _real_generate
_CRITIQUE: CritiqueFn = _real_critique
_TTS: TtsFn = _real_tts


def run_generate_podcast_job(job, session: Session) -> int:
    """Worker-facing wrapper bound to the module seams (called by the `generate` handler)."""
    return run_generate_podcast(job, session, generate=_GENERATE, critique=_CRITIQUE, tts=_TTS)
