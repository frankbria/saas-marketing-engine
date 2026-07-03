# Ephemeral GPU media-worker image (S5.0, issue #28).
#
# One pinned image, registered as a RunPod template (its id -> SME_GPU_POD_TEMPLATE_ID);
# the provisioner boots pods from it when `media` jobs are pending and destroys them when
# the queue goes idle. The worker connects OUT to the VPS Redis broker — the broker URL
# arrives via the pod template env (SME_CELERY_BROKER_URL); see README.md for the
# transport security requirements (never a raw internet-exposed Redis).
#
# Build from the REPO ROOT (the backend package is the context):
#   docker build -f infra/gpu-worker/Dockerfile -t sme-media-worker:<version> .
#
# ffmpeg is the media dep every S5.x pipeline needs; the heavyweight generators
# (ACE-Step S5.2, manim/Remotion S5.1) layer on here when their stories land with real
# version pins — guessing their pins now would be a lie about reproducibility.

FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv (pinned) installs the exact backend lockfile — the image is only reproducible
# against ==-pinned deps (enforced by test_phase_b_queue_deps_are_pinned).
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

WORKDIR /srv/sme
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/app ./app

ENV PATH="/srv/sme/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Exactly the media queue, one job at a time (acks_late + prefetch 1 in celery_app.py do
# the delivery semantics; concurrency=1 keeps one GPU job per pod — scaling is 0<->1 pods,
# not threads).
CMD ["celery", "-A", "app.celery_app", "worker", "-Q", "media", "--concurrency=1", "--loglevel=INFO"]
