"""Verify installed Messages/config/embedding integration using only synthetic data."""

import asyncio
import json
import secrets
import tempfile
from pathlib import Path

import httpx
from cloud_memory_hub.secret_store import EncryptedSecretStore
from cryptography.fernet import Fernet
from hy_memory.agent.llm_provider import LLMProvider
from one_memory.hy_memory_bridge import build_hy_memory_config
from one_memory.model_probe import ModelProber
from one_memory.model_profiles import ModelProfileInput, ModelProfileStore


async def probe(root):
    key = secrets.token_urlsafe(40)
    encryption_key = Fernet.generate_key()
    vault = EncryptedSecretStore(root / "credentials.sqlite3", encryption_key)
    store = ModelProfileStore(root / "profiles.sqlite3")
    profile = store.create(
        ModelProfileInput(
            name="Synthetic Messages installation probe",
            provider_kind="anthropic",
            base_url="https://messages.example.invalid",
            model="synthetic-model",
            api_key=key,
            max_concurrency=1,
        ),
        vault,
    )
    assert profile.has_api_key and not profile.is_active
    assert key not in profile.model_dump_json()
    requests = []

    def respond(request):
        assert request.method == "POST"
        assert str(request.url) == "https://messages.example.invalid/v1/messages"
        assert request.headers["x-api-key"] == key
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == "synthetic-model"
        assert "response_format" not in body and "temperature" not in body
        assert body["max_tokens"] > 0 and body["messages"]
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_synthetic",
                "type": "message",
                "role": "assistant",
                "model": "synthetic-model",
                "content": [{"type": "text", "text": '{"memory_probe": true}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 6},
            },
        )

    result = await ModelProber(
        store=store, secrets=vault, transport=httpx.MockTransport(respond)
    ).probe(profile.profile_id)
    assert result.capabilities.endpoint_reachable
    assert result.capabilities.model_available
    assert result.capabilities.embedding_available
    assert not result.capabilities.structured_output
    assert len(requests) == 1
    active = store.activate(profile.profile_id, vault)
    assert active.is_active and active.embedding_provider_kind == "local_onnx"
    config = build_hy_memory_config(
        store.snapshot(active.profile_id, active.revision), api_key=key, data_dir=root / "memory"
    )
    config.http_transport_factory = lambda: httpx.MockTransport(respond)
    provider = LLMProvider(config)
    try:
        completion = await provider.complete("Return memory_probe=true as JSON", max_tokens=64)
        assert json.loads(completion.content) == {"memory_probe": True}
        assert (completion.prompt_tokens, completion.completion_tokens) == (10, 6)
        assert completion.finish_reason == "stop" and len(requests) == 2
    finally:
        await provider.close()
    reopened_vault = EncryptedSecretStore(root / "credentials.sqlite3", encryption_key)
    reopened = ModelProfileStore(root / "profiles.sqlite3")
    assert (
        reopened.resolve_snapshot_secret(active.profile_id, active.revision, reopened_vault) == key
    )
    assert key.encode() not in (root / "credentials.sqlite3").read_bytes()
    print(
        json.dumps(
            {
                "result": "passed",
                "provider": "anthropic",
                "messages_calls": len(requests),
                "builtin_embedding": "actual_local_encode",
                "credentials": "encrypted_reopen_passed",
                "external_model": "not_tested_synthetic_transport",
            }
        )
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cmh-anthropic-probe-") as directory:
        asyncio.run(probe(Path(directory)))
