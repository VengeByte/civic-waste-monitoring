"""
Unit tests for the config-driven VLM router.

Covers provider configuration, payload capabilities, error classification, provider
and model fallback, retry semantics, auth/dead-model circuit handling, and metrics.
"""

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.circuit_breaker import CIRCUIT_BREAKER, provider_key
from backend.vlm_config import (
    DEFAULT_PROVIDERS,
    ProviderConfigError,
    VLMModelConfig,
    VLMProviderConfig,
    load_provider_configs,
)
from backend.vlm_metrics import VLM_METRICS
from backend.vlm_router import (
    AllVLMProvidersFailedError,
    FailureKind,
    VLMResult,
    analyze_with_vlm,
    build_chat_completion_payload,
    classify_http_failure,
    extract_and_validate_json,
)


def make_provider(
    name: str,
    endpoint: str,
    api_key_env: str,
    models: list[str],
    *,
    retries: int = 0,
    timeout: float = 10.0,
) -> VLMProviderConfig:
    """Build a small provider config for routing tests."""
    return VLMProviderConfig(
        name=name,
        endpoint=endpoint,
        api_key_env=api_key_env,
        models=tuple(VLMModelConfig(id=model) for model in models),
        timeout_seconds=timeout,
        max_retries=retries,
    )


def valid_payload(severity: str = "medium") -> dict:
    """Build a valid provider response payload."""
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "garbage_detected": True,
                            "confidence": 0.95,
                            "waste_types": ["plastic bags"],
                            "severity": severity,
                            "official_complaint_description": "Refuse is blocking the pavement.",
                        }
                    )
                }
            }
        ]
    }


class TestVLMConfigAndSchema(unittest.TestCase):
    def test_default_providers_configuration(self):
        """Verify the two configured providers, model order, and env-var-only keys."""
        self.assertEqual(len(DEFAULT_PROVIDERS), 2)

        gemini = DEFAULT_PROVIDERS[0]
        self.assertEqual(gemini.name, "Google Gemini")
        self.assertEqual(
            gemini.endpoint,
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )
        self.assertEqual(gemini.api_key_env, "GEMINI_API_KEY")
        self.assertEqual(gemini.model_ids[0], "gemini-3.6-flash")
        self.assertGreaterEqual(len(gemini.model_ids), 2)
        self.assertGreaterEqual(gemini.max_retries, 1)

        openrouter = DEFAULT_PROVIDERS[1]
        self.assertEqual(openrouter.name, "OpenRouter")
        self.assertEqual(openrouter.api_key_env, "OPENROUTER_API_KEY")
        self.assertEqual(
            openrouter.model_ids[0], "nvidia/nemotron-3.5-super-vl:free"
        )

        # Groq currently has no vision model in its catalog, so it must be absent.
        self.assertFalse(any("groq" in provider.name.lower() for provider in DEFAULT_PROVIDERS))
        for provider in DEFAULT_PROVIDERS:
            self.assertTrue(provider.api_key_env.isupper())
            self.assertNotIn("sk-", provider.api_key_env)

    def _write_config(self, name: str, payload: dict) -> Path:
        """Write a temporary provider config inside the workspace (sandbox-safe)."""
        path = Path(__file__).resolve().parent / f"_tmp_provider_{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_config_loader_rejects_literal_api_key(self):
        """The loader must refuse a config that embeds a key instead of an env name."""
        path = self._write_config(
            "literal_key",
            {
                "providers": [
                    {
                        "name": "Bad",
                        "endpoint": "https://bad.test/v1/chat/completions",
                        "api_key_env": "sk-live-1234567890abcdef",
                        "models": [{"id": "vision-model"}],
                    }
                ]
            },
        )
        with self.assertRaises(ProviderConfigError):
            load_provider_configs(path)

    def test_config_loader_accepts_valid_file(self):
        """A valid alternative config loads with model-level JSON capabilities."""
        path = self._write_config(
            "valid",
            {
                "providers": [
                    {
                        "name": "Example",
                        "endpoint": "https://example.test/v1/chat/completions",
                        "api_key_env": "EXAMPLE_API_KEY",
                        "timeout_seconds": 7,
                        "max_retries": 0,
                        "models": [
                            {"id": "vision-a", "json_mode": True},
                            {"id": "vision-b", "json_mode": False},
                        ],
                    }
                ]
            },
        )
        providers = load_provider_configs(path)

        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].model_ids, ("vision-a", "vision-b"))
        self.assertFalse(providers[0].models[1].json_mode)

    def test_schema_validation_valid(self):
        parsed = extract_and_validate_json(
            json.dumps(
                {
                    "garbage_detected": True,
                    "confidence": 0.95,
                    "waste_types": ["plastic bags", "domestic waste"],
                    "severity": "medium",
                    "official_complaint_description": "Multiple garbage bags are overflowing.",
                }
            )
        )
        self.assertTrue(parsed["garbage_detected"])
        self.assertEqual(parsed["confidence"], 0.95)
        self.assertEqual(parsed["severity"], "medium")

    def test_schema_validation_markdown_code_fences(self):
        fenced = (
            "```json\n"
            '{"garbage_detected": false, "confidence": 0.99, "waste_types": [], '
            '"severity": "low", "official_complaint_description": "Clean area."}\n'
            "```"
        )
        parsed = extract_and_validate_json(fenced)
        self.assertFalse(parsed["garbage_detected"])
        self.assertEqual(parsed["waste_types"], [])

    def test_schema_validation_invalid_data(self):
        with self.assertRaises(ValueError):
            extract_and_validate_json('{"garbage_detected": true}')

        with self.assertRaises(ValueError):
            extract_and_validate_json(
                json.dumps(
                    {
                        "garbage_detected": True,
                        "confidence": 0.8,
                        "waste_types": ["cans"],
                        "severity": "catastrophic",
                        "official_complaint_description": "Garbage found.",
                    }
                )
            )

        with self.assertRaises(ValueError):
            extract_and_validate_json(
                json.dumps(
                    {
                        "garbage_detected": "yes",
                        "confidence": 0.8,
                        "waste_types": ["cans"],
                        "severity": "low",
                        "official_complaint_description": "Garbage found.",
                    }
                )
            )

    def test_payload_respects_json_mode_capability(self):
        with_json = build_chat_completion_payload(b"image", "model-a", json_mode=True)
        without_json = build_chat_completion_payload(b"image", "model-b", json_mode=False)

        self.assertEqual(with_json["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", without_json)
        self.assertEqual(with_json["temperature"], 0.0)
        self.assertGreaterEqual(with_json["max_tokens"], 1000)

    def test_error_classification(self):
        self.assertEqual(classify_http_failure(401, ""), FailureKind.AUTH)
        self.assertEqual(classify_http_failure(403, ""), FailureKind.AUTH)
        self.assertEqual(
            classify_http_failure(400, "model has been decommissioned"),
            FailureKind.DEAD_MODEL,
        )
        self.assertEqual(
            classify_http_failure(404, "no provider serves this model"),
            FailureKind.DEAD_MODEL,
        )
        self.assertEqual(classify_http_failure(429, ""), FailureKind.RATE_LIMIT)
        self.assertEqual(classify_http_failure(503, "high demand"), FailureKind.UPSTREAM)
        self.assertEqual(classify_http_failure(400, "bad response_format"), FailureKind.BAD_REQUEST)


class TestVLMFallbackExecution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 200

    def test_primary_provider_success(self):
        """Success on the first attempt returns metadata and records metrics."""
        provider = make_provider(
            "Primary", "https://primary.test/v1/chat/completions", "PRIMARY_TEST_KEY", ["vision-1"]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=valid_payload("high"))

        async def _run():
            with patch.dict(os.environ, {"PRIMARY_TEST_KEY": "fake"}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    result = await analyze_with_vlm(
                        self.sample_bytes, providers=(provider,), client=client
                    )

            self.assertEqual(result.provider, "Primary")
            self.assertEqual(result.model, "vision-1")
            self.assertFalse(result.fallback_used)
            self.assertEqual(result["severity"], "high")
            self.assertEqual(len(result.attempts), 1)
            self.assertEqual(VLM_METRICS.snapshot()["successes"], 1)

        asyncio.run(_run())

    def test_fallback_on_429_rate_limit(self):
        """A 429 on the primary moves to the secondary provider."""
        primary = make_provider(
            "Primary", "https://primary.test/v1/chat/completions", "PRIMARY_TEST_KEY", ["vision-1"]
        )
        secondary = make_provider(
            "Secondary", "https://secondary.test/v1/chat/completions", "SECONDARY_TEST_KEY", ["vision-2"]
        )
        attempted = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempted.append(request.url.host)
            if request.url.host == "primary.test":
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json=valid_payload())

        async def _run():
            with patch.dict(
                os.environ,
                {"PRIMARY_TEST_KEY": "fake", "SECONDARY_TEST_KEY": "fake"},
            ):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    result = await analyze_with_vlm(
                        self.sample_bytes, providers=(primary, secondary), client=client
                    )

            self.assertEqual(result.provider, "Secondary")
            self.assertTrue(result.fallback_used)
            self.assertEqual(attempted, ["primary.test", "secondary.test"])

        asyncio.run(_run())

    def test_retry_on_503_then_success(self):
        """A transient 503 is retried once before falling through."""
        provider = make_provider(
            "RetryProvider",
            "https://retry.test/v1/chat/completions",
            "RETRY_TEST_KEY",
            ["vision-1"],
            retries=1,
        )
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(503, text="high demand")
            return httpx.Response(200, json=valid_payload())

        async def _run():
            with patch.dict(os.environ, {"RETRY_TEST_KEY": "fake"}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    result = await analyze_with_vlm(
                        self.sample_bytes, providers=(provider,), client=client
                    )

            self.assertEqual(calls["count"], 2)
            self.assertEqual(result.provider, "RetryProvider")
            self.assertEqual(VLM_METRICS.snapshot()["retries"], 1)

        asyncio.run(_run())

    def test_dead_model_404_skips_to_next_model(self):
        """A retired model is skipped in favour of the next configured model."""
        provider = make_provider(
            "ModelFallback",
            "https://models.test/v1/chat/completions",
            "MODEL_TEST_KEY",
            ["retired-model", "working-model"],
        )
        attempted_models = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            attempted_models.append(model)
            if model == "retired-model":
                return httpx.Response(
                    404, json={"error": {"message": "no longer available"}}
                )
            return httpx.Response(200, json=valid_payload())

        async def _run():
            with patch.dict(os.environ, {"MODEL_TEST_KEY": "fake"}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    result = await analyze_with_vlm(
                        self.sample_bytes, providers=(provider,), client=client
                    )

            self.assertEqual(attempted_models, ["retired-model", "working-model"])
            self.assertEqual(result.model, "working-model")

        asyncio.run(_run())

    def test_auth_failure_opens_provider_circuit(self):
        """A 401 skips the provider's remaining models and opens its circuit."""
        provider = make_provider(
            "AuthProvider",
            "https://auth.test/v1/chat/completions",
            "AUTH_TEST_KEY",
            ["model-1", "model-2"],
        )
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(401, json={"error": "invalid api key"})

        async def _run():
            with patch.dict(os.environ, {"AUTH_TEST_KEY": "fake"}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(AllVLMProvidersFailedError):
                        await analyze_with_vlm(
                            self.sample_bytes, providers=(provider,), client=client
                        )

                    # The circuit is now open; a second call must not touch HTTP.
                    with self.assertRaises(AllVLMProvidersFailedError):
                        await analyze_with_vlm(
                            self.sample_bytes, providers=(provider,), client=client
                        )

            self.assertEqual(calls["count"], 1)
            self.assertFalse(CIRCUIT_BREAKER.is_allowed(provider_key("AuthProvider")))

        asyncio.run(_run())

    def test_invalid_json_retries_then_falls_back(self):
        """A 200 with invalid JSON is a bad-output failure, not a success."""
        primary = make_provider(
            "BadOutput",
            "https://badoutput.test/v1/chat/completions",
            "BAD_OUTPUT_KEY",
            ["vision-1"],
            retries=1,
        )
        secondary = make_provider(
            "GoodOutput",
            "https://goodoutput.test/v1/chat/completions",
            "GOOD_OUTPUT_KEY",
            ["vision-2"],
        )
        calls = {"primary": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "badoutput.test":
                calls["primary"] += 1
                return httpx.Response(
                    200, json={"choices": [{"message": {"content": "not json"}}]}
                )
            return httpx.Response(200, json=valid_payload())

        async def _run():
            with patch.dict(
                os.environ, {"BAD_OUTPUT_KEY": "fake", "GOOD_OUTPUT_KEY": "fake"}
            ):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    result = await analyze_with_vlm(
                        self.sample_bytes, providers=(primary, secondary), client=client
                    )

            self.assertEqual(calls["primary"], 2)  # initial + one retry
            self.assertEqual(result.provider, "GoodOutput")

        asyncio.run(_run())

    def test_all_providers_failed_raises_exception(self):
        """Every retryable failure still ends in a clean AllVLMProvidersFailedError."""
        provider = make_provider(
            "AlwaysFails",
            "https://fail.test/v1/chat/completions",
            "FAIL_TEST_KEY",
            ["vision-1"],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        async def _run():
            with patch.dict(os.environ, {"FAIL_TEST_KEY": "fake"}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(AllVLMProvidersFailedError):
                        await analyze_with_vlm(
                            self.sample_bytes, providers=(provider,), client=client
                        )

        asyncio.run(_run())

    def test_missing_key_is_skipped_without_http(self):
        """A provider without its environment variable is never called."""
        provider = make_provider(
            "NoKey", "https://nokey.test/v1/chat/completions", "DEFINITELY_MISSING_KEY", ["vision-1"]
        )

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("HTTP should not be called without a key")

        async def _run():
            os.environ.pop("DEFINITELY_MISSING_KEY", None)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(AllVLMProvidersFailedError) as context:
                    await analyze_with_vlm(
                        self.sample_bytes, providers=(provider,), client=client
                    )
            self.assertIn("missing API key", str(context.exception))

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
