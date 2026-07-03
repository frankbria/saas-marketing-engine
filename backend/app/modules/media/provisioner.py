"""Ephemeral GPU provisioner (S5.0, issue #28).

A thin provider-agnostic protocol with one commercial implementation (RunPod REST API).
The orchestration loop decides *when* to boot/tear down; this module only knows *how*.
The provider API key comes from env (`SME_GPU_API_KEY`, §9 — never the DB) and is
registered for log redaction the moment it's used.

ponytail: one provider, 0↔1 pods, adoption-by-name instead of a pod registry. Multi-
provider failover and autoscaling are explicit non-goals (issue #28).
"""

from enum import StrEnum
from typing import Protocol

import httpx

from app.config import settings
from app.secrets.vault import register_secret

RUNPOD_BASE_URL = "https://rest.runpod.io/v1"
# All provisioner-managed pods carry this name — it's how a pod orphaned between
# "provider created it" and "lease row committed" gets adopted instead of duplicated.
POD_NAME = "sme-media-worker"


class PodState(StrEnum):
    NONE = "none"  # no provisioner-managed pod exists at the provider
    STARTING = "starting"
    RUNNING = "running"


class GpuProvisioner(Protocol):
    """What the orchestration loop needs from any GPU provider."""

    def ensure_worker(self) -> str:
        """Boot (or adopt) the worker pod; return its provider pod id. Idempotent."""
        ...

    def status(self) -> PodState:
        """Current state of the provisioner-managed pod at the provider."""
        ...

    def teardown(self, pod_id: str) -> bool:
        """Destroy the pod. True only when verified gone at the provider (billing
        stopped); False means the caller must alert, not assume."""
        ...


class RunPodProvisioner:
    """RunPod REST implementation (POST/GET/DELETE /v1/pods)."""

    def __init__(self, template_id: str | None, client: httpx.Client):
        self._template_id = template_id
        self._client = client

    def ensure_worker(self) -> str:
        existing = self._find_existing()
        if existing is not None:
            return existing["id"]
        if not self._template_id:
            raise RuntimeError(
                "SME_GPU_POD_TEMPLATE_ID is not set — register the infra/gpu-worker image "
                "as a RunPod template and set its id before provisioning"
            )
        resp = self._client.post(
            "/pods",
            json={"name": POD_NAME, "templateId": self._template_id, "computeType": "GPU"},
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def status(self) -> PodState:
        pod = self._find_existing()
        if pod is None:
            return PodState.NONE
        return PodState.RUNNING if pod.get("desiredStatus") == "RUNNING" else PodState.STARTING

    def teardown(self, pod_id: str) -> bool:
        resp = self._client.delete(f"/pods/{pod_id}")
        # 404 = the pod is already gone (spot loss / manual removal) — that IS the desired
        # end state, not an error; raising here would strand the lease ACTIVE forever.
        if resp.status_code != 404:
            resp.raise_for_status()
        # Verify destruction — "pod destroyed, billing stopped" is the acceptance
        # criterion, and a provider that quietly kept the pod would bill forever.
        return self._client.get(f"/pods/{pod_id}").status_code == 404

    def _find_existing(self) -> dict | None:
        resp = self._client.get("/pods")
        resp.raise_for_status()
        return next((p for p in resp.json() if p.get("name") == POD_NAME), None)


def build_provider() -> GpuProvisioner:
    """Resolve the configured provider. The orchestrator calls through this seam;
    tests monkeypatch it (the `_build_reddit` pattern)."""
    if settings.gpu_provider != "runpod":
        raise RuntimeError(
            f"unknown gpu_provider {settings.gpu_provider!r} — only 'runpod' is implemented"
        )
    key = settings.gpu_api_key.get_secret_value() if settings.gpu_api_key else None
    if not key:
        raise RuntimeError("SME_GPU_API_KEY is not set — cannot provision the media GPU worker")
    register_secret(key)  # §9: the key must never appear in a log line
    client = httpx.Client(
        base_url=RUNPOD_BASE_URL,
        headers={"Authorization": f"Bearer {key}"},
        timeout=30.0,
    )
    return RunPodProvisioner(template_id=settings.gpu_pod_template_id, client=client)
