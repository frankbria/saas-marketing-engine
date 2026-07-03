"""S5.0: GPU provisioner — provider-agnostic interface + RunPod implementation (issue #28).

The provider API is faked at the HTTP boundary (httpx.MockTransport + a hand-written
in-memory RunPod: pods as a dict), so RunPodProvisioner's real request/response handling
runs unmodified. No Redis/Celery involved at this layer.
"""

import httpx
import pytest

from app.config import settings
from app.modules.media import provisioner as prov_mod
from app.modules.media.provisioner import (
    POD_NAME,
    PodState,
    RunPodProvisioner,
    build_provider,
)


class _FakeRunPodApi:
    """In-memory stand-in for rest.runpod.io/v1 — records calls, serves pod CRUD."""

    def __init__(self):
        self.pods: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self._next_id = 0
        self.fail_all = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self.fail_all:
            return httpx.Response(500, json={"error": "provider exploded"})
        if request.headers.get("Authorization") != "Bearer rp_test_key":
            return httpx.Response(401, json={"error": "unauthorized"})
        path = request.url.path
        if request.method == "POST" and path == "/v1/pods":
            body = __import__("json").loads(request.content)
            self._next_id += 1
            pod = {"id": f"pod-{self._next_id}", "name": body["name"], "desiredStatus": "RUNNING"}
            self.pods[pod["id"]] = pod
            return httpx.Response(201, json=pod)
        if request.method == "GET" and path == "/v1/pods":
            return httpx.Response(200, json=list(self.pods.values()))
        if path.startswith("/v1/pods/"):
            pod_id = path.removeprefix("/v1/pods/")
            if request.method == "GET":
                pod = self.pods.get(pod_id)
                return httpx.Response(200, json=pod) if pod else httpx.Response(404)
            if request.method == "DELETE":
                self.pods.pop(pod_id, None)
                return httpx.Response(200, json={})
        return httpx.Response(404)


@pytest.fixture
def fake_api():
    return _FakeRunPodApi()


@pytest.fixture
def provider(fake_api):
    client = httpx.Client(
        base_url="https://rest.runpod.io/v1",
        transport=httpx.MockTransport(fake_api.handler),
        headers={"Authorization": "Bearer rp_test_key"},
    )
    return RunPodProvisioner(template_id="tmpl-123", client=client)


def test_ensure_worker_creates_pod(provider, fake_api):
    pod_id = provider.ensure_worker()
    assert pod_id in fake_api.pods
    assert fake_api.pods[pod_id]["name"] == POD_NAME
    assert ("POST", "/v1/pods") in fake_api.requests


def test_ensure_worker_adopts_existing_pod(provider, fake_api):
    # A pod created just before a crash (lease not yet committed) must be adopted by name,
    # never double-created — a second pod would be a silent billing leak.
    first = provider.ensure_worker()
    second = provider.ensure_worker()
    assert first == second
    assert len(fake_api.pods) == 1


def test_ensure_worker_without_template_fails_loudly(fake_api):
    client = httpx.Client(
        base_url="https://rest.runpod.io/v1",
        transport=httpx.MockTransport(fake_api.handler),
        headers={"Authorization": "Bearer rp_test_key"},
    )
    provider = RunPodProvisioner(template_id=None, client=client)
    with pytest.raises(RuntimeError, match="SME_GPU_POD_TEMPLATE_ID"):
        provider.ensure_worker()


def test_status_reflects_pod_lifecycle(provider):
    assert provider.status() is PodState.NONE
    provider.ensure_worker()
    assert provider.status() is PodState.RUNNING


def test_teardown_verifies_destroyed(provider, fake_api):
    pod_id = provider.ensure_worker()
    assert provider.teardown(pod_id) is True  # DELETE then GET → 404 = billing stopped
    assert fake_api.pods == {}
    assert ("DELETE", f"/v1/pods/{pod_id}") in fake_api.requests
    assert ("GET", f"/v1/pods/{pod_id}") in fake_api.requests


def test_teardown_reports_unverified_when_pod_survives(provider, fake_api):
    pod_id = provider.ensure_worker()

    # Simulate a provider that accepts the DELETE but doesn't actually destroy the pod.
    original = fake_api.handler

    def keep_pod(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            fake_api.requests.append((request.method, request.url.path))
            return httpx.Response(200, json={})
        return original(request)

    provider._client._transport = httpx.MockTransport(keep_pod)
    assert provider.teardown(pod_id) is False  # caller must alert, not assume billing stopped


def test_provider_error_raises(provider, fake_api):
    fake_api.fail_all = True
    with pytest.raises(httpx.HTTPStatusError):
        provider.ensure_worker()


def test_build_provider_requires_api_key(monkeypatch):
    monkeypatch.setattr(settings, "gpu_api_key", None)
    with pytest.raises(RuntimeError, match="SME_GPU_API_KEY"):
        build_provider()


def test_build_provider_rejects_unknown_provider(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "gpu_api_key", SecretStr("rp_test_key"))
    monkeypatch.setattr(settings, "gpu_provider", "aws")
    with pytest.raises(RuntimeError, match="aws"):
        build_provider()


def test_build_provider_registers_key_for_redaction(monkeypatch):
    from pydantic import SecretStr

    from app.secrets.vault import redact

    monkeypatch.setattr(settings, "gpu_api_key", SecretStr("rp_redact_me_9x7"))
    monkeypatch.setattr(settings, "gpu_provider", "runpod")
    build_provider()
    # The key must never appear in any log line (§9) — redact() proves registration.
    assert "rp_redact_me_9x7" not in redact("boot failed: token rp_redact_me_9x7 rejected")


def test_build_provider_seam_is_monkeypatchable(monkeypatch):
    # The orchestrator resolves its provider through this seam; tests swap in fakes here
    # (same pattern as app.channels.reddit._build_reddit).
    sentinel = object()
    monkeypatch.setattr(prov_mod, "build_provider", lambda: sentinel)
    assert prov_mod.build_provider() is sentinel
