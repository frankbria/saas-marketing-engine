"""S5.1: short-form video composition on the GPU plane (issue #29).

`render_video` is the render step of the short-form pipeline: it turns a script (title +
captioned segments) and a narration MP3 into a vertical MP4, with each caption burned over
its equal share of the narration and the audio muxed in. It is a **pure** function — no DB,
no broker, no app settings — so it runs unchanged on the ephemeral rented GPU pod, which
must not depend on VPS config (TECH_SPEC Phase B). The caller (`media.render_video` on the
GPU `media` queue) passes `max_bytes` in explicitly for the same reason.

This ffmpeg composition is the SEAM: v1 draws captions on a solid background to prove the
plumbing, and heavyweight renderers (manim / Remotion) slot in behind this signature later
without changing callers. Output is returned as a base64 string because Celery's JSON
serializer can't carry raw bytes.
"""

from __future__ import annotations

import base64
import binascii
import os
import subprocess
import tempfile

# DejaVu ships in the GPU worker image (fonts-dejavu-core); the extra paths cover other
# distros. drawtext needs a real font file — we fail loudly rather than let ffmpeg fall
# back to something absent.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

# Hard bounds on the subprocess calls: the worker runs --concurrency=1, so a hung ffmpeg/ffprobe
# would wedge the whole pod — and a busy-looking worker is never torn down by the provisioner,
# turning one bad encode into unbounded paid GPU time. TimeoutExpired propagates like any other
# failure (Celery autoretry → bounded by the tick's dispatch cap). 10 min covers a slow software
# encode of a short-form clip with a wide margin.
_PROBE_TIMEOUT_SECONDS = 60
_RENDER_TIMEOUT_SECONDS = 600


def _find_font() -> str:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError(
        "no usable font found for drawtext; install fonts-dejavu-core "
        f"(looked in {', '.join(_FONT_CANDIDATES)})"
    )


def _probe_duration(mp3_path: str) -> float:
    """Return the audio duration in seconds via ffprobe, raising on unreadable input."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            mp3_path,
        ],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"ffprobe could not read narration: {proc.stderr.strip()[-500:]}")
    return float(proc.stdout.strip())


def render_video(
    script: dict,
    narration_b64: str,
    *,
    max_bytes: int,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """Compose a vertical MP4 from a script and its narration, returned base64-encoded.

    `script` is ``{"title": str, "segments": [{"caption": str, "narration": str}, ...]}``.
    Each segment's caption is drawn (ffmpeg drawtext) over an equal slice of the narration's
    total duration on a solid background, then muxed with the audio (`-shortest`). Raises
    ``ValueError`` for empty segments, unreadable narration, or output exceeding
    ``max_bytes``; ``RuntimeError`` if ffmpeg/ffprobe fail or no font is available.
    """
    segments = script.get("segments") or []
    if not segments:
        raise ValueError("script has no segments to render")

    try:
        narration = base64.b64decode(narration_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"narration_b64 is not valid base64: {exc}") from exc
    if not narration:
        raise ValueError("narration_b64 decoded to empty audio")

    font = _find_font()

    with tempfile.TemporaryDirectory() as tmp:
        mp3_path = os.path.join(tmp, "narration.mp3")
        with open(mp3_path, "wb") as fh:
            fh.write(narration)

        duration = _probe_duration(mp3_path)
        share = duration / len(segments)

        # textfile= (one temp file per caption) sidesteps drawtext's inline-text escaping
        # bugs entirely — the caption bytes never touch the filter string.
        drawtexts = []
        for i, segment in enumerate(segments):
            caption_path = os.path.join(tmp, f"caption_{i}.txt")
            with open(caption_path, "w", encoding="utf-8") as fh:
                fh.write(str(segment.get("caption", "")))
            start = i * share
            end = (i + 1) * share
            drawtexts.append(
                f"drawtext=fontfile={font}:textfile={caption_path}"
                ":fontcolor=white:fontsize=64:line_spacing=12"
                ":x=(w-text_w)/2:y=(h-text_h)/2"
                f":enable='between(t,{start:.3f},{end:.3f})'"
            )
        filter_complex = f"[0:v]{','.join(drawtexts)}[v]"

        out_path = os.path.join(tmp, "out.mp4")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            # Solid-colour background sized to the narration; -shortest still trims to the
            # audio, but a bounded source keeps ffmpeg from generating video forever.
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:d={duration:.3f}",
            "-i",
            mp3_path,
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            # Strip metadata for a reproducible output (no encoder timestamps/tags).
            "-map_metadata",
            "-1",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=_RENDER_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg render failed (code {proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()[-1000:]}"
            )

        size = os.path.getsize(out_path)
        if size > max_bytes:
            raise ValueError(f"rendered video {size} bytes exceeds max_bytes {max_bytes}")

        with open(out_path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
