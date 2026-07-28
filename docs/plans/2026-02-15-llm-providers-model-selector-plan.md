# Multi-Provider LLM Support + Auto-Populated Model Selector — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add DeepSeek, Mistral, OpenAI, and Gemini LLM providers via an OpenAI-compatible base class, and auto-populate the model selector dropdown from each provider's API with persistent caching.

**Architecture:** An `OpenAICompatibleProvider` base class in bmlib uses the `openai` Python SDK with configurable `base_url`. Four thin subclasses override metadata, pricing, and fallback model lists. The BioMedicalNews settings UI gets an HTMX-driven model `<datalist>` that fetches models on provider change and page load, caching results to `~/.bmnews/model_cache.json`.

**Tech Stack:** Python 3.12, `openai` SDK, Flask/HTMX, Jinja2 templates, TOML config, pytest

---

### Task 1: OpenAI-Compatible Base Provider

**Files:**
- Create: `bmlib/llm/providers/openai_compat.py`
- Test: `bmlib/tests/test_openai_compat.py`

**Step 1: Write the test file**

Create `bmlib/tests/test_openai_compat.py`:

```python
# bmlib — shared library for biomedical literature tools
# Copyright (C) 2024-2026 Dr Horst Herb
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Tests for OpenAI-compatible base provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bmlib.llm.data_types import LLMMessage
from bmlib.llm.providers.base import ModelMetadata, ModelPricing, ProviderCapabilities


class _StubProvider:
    """Minimal concrete subclass for testing the base class."""

    PROVIDER_NAME = "stub"
    DISPLAY_NAME = "Stub"
    DESCRIPTION = "Test stub"
    WEBSITE_URL = "https://stub.test"
    SETUP_INSTRUCTIONS = "N/A"
    API_KEY_ENV_VAR = "STUB_API_KEY"
    DEFAULT_BASE_URL = "https://api.stub.test/v1"
    DEFAULT_MODEL = "stub-model"
    FALLBACK_MODELS = [
        ModelMetadata(
            model_id="stub-model",
            display_name="Stub Model",
            context_window=128_000,
            pricing=ModelPricing(1.0, 2.0),
        ),
    ]
    MODEL_PRICING = {
        "stub-model": ModelPricing(1.0, 2.0),
    }


# Defer import so we can define _StubProvider first, then subclass
@pytest.fixture
def StubProvider():
    from bmlib.llm.providers.openai_compat import OpenAICompatibleProvider

    class _Impl(OpenAICompatibleProvider, _StubProvider):
        pass

    return _Impl


class TestProperties:
    def test_is_not_local(self, StubProvider):
        p = StubProvider(api_key="test-key")
        assert p.is_local is False
        assert p.is_free is False
        assert p.requires_api_key is True

    def test_api_key_env_var(self, StubProvider):
        p = StubProvider(api_key="k")
        assert p.api_key_env_var == "STUB_API_KEY"

    def test_default_model(self, StubProvider):
        p = StubProvider(api_key="k")
        assert p.default_model == "stub-model"

    def test_default_base_url(self, StubProvider):
        p = StubProvider(api_key="k")
        assert p.default_base_url == "https://api.stub.test/v1"


class TestChat:
    def test_chat_routes_to_openai_sdk(self, StubProvider):
        p = StubProvider(api_key="test-key")

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Hello back"
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.choices[0].finish_reason = "stop"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        p._client = mock_client

        msgs = [LLMMessage(role="user", content="Hello")]
        result = p.chat(msgs, model="stub-model")

        assert result.content == "Hello back"
        assert result.input_tokens == 10
        assert result.output_tokens == 5

        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == "stub-model"

    def test_chat_separates_system_message(self, StubProvider):
        p = StubProvider(api_key="test-key")

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "OK"
        mock_response.usage.prompt_tokens = 20
        mock_response.usage.completion_tokens = 1
        mock_response.choices[0].finish_reason = "stop"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        p._client = mock_client

        msgs = [
            LLMMessage(role="system", content="Be helpful"),
            LLMMessage(role="user", content="Hi"),
        ]
        p.chat(msgs)

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        sent_messages = call_kwargs["messages"]
        # System message should be included as a regular message for OpenAI-compat
        assert any(m["role"] == "system" for m in sent_messages)


class TestListModels:
    def test_list_models_from_api(self, StubProvider):
        p = StubProvider(api_key="test-key")

        mock_model = MagicMock()
        mock_model.id = "stub-model"

        mock_client = MagicMock()
        mock_client.models.list.return_value.data = [mock_model]
        p._client = mock_client

        models = p.list_models()
        assert len(models) >= 1
        assert models[0].model_id == "stub-model"

    def test_list_models_fallback_on_error(self, StubProvider):
        p = StubProvider(api_key="test-key")

        mock_client = MagicMock()
        mock_client.models.list.side_effect = Exception("API error")
        p._client = mock_client

        models = p.list_models()
        assert len(models) == 1
        assert models[0].model_id == "stub-model"


class TestTokenCounting:
    def test_count_tokens_estimation(self, StubProvider):
        p = StubProvider(api_key="k")
        count = p.count_tokens("Hello world, this is a test.")
        assert count > 0
        assert isinstance(count, int)


class TestConnectionTest:
    def test_connection_success(self, StubProvider):
        p = StubProvider(api_key="test-key")

        mock_model = MagicMock()
        mock_model.id = "m1"
        mock_client = MagicMock()
        mock_client.models.list.return_value.data = [mock_model]
        p._client = mock_client

        ok, msg = p.test_connection()
        assert ok is True

    def test_connection_failure(self, StubProvider):
        p = StubProvider(api_key="test-key")

        mock_client = MagicMock()
        mock_client.models.list.side_effect = Exception("Connection refused")
        p._client = mock_client

        ok, msg = p.test_connection()
        assert ok is False
        assert "Connection refused" in msg
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_openai_compat.py -v`
Expected: FAIL — `openai_compat` module does not exist

**Step 3: Write the OpenAI-compatible base provider**

Create `bmlib/llm/providers/openai_compat.py`:

```python
# bmlib — shared library for biomedical literature tools
# Copyright (C) 2024-2026 Dr Horst Herb
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Base class for providers that expose an OpenAI-compatible chat API.

Subclasses set class-level constants for provider metadata, base URL,
API key env var, default model, fallback model list, and pricing dict.
The ``openai`` Python SDK handles the actual HTTP calls.

Usage::

    class MyProvider(OpenAICompatibleProvider):
        PROVIDER_NAME = "myprovider"
        DISPLAY_NAME = "My Provider"
        DESCRIPTION = "My LLM provider"
        WEBSITE_URL = "https://myprovider.ai"
        SETUP_INSTRUCTIONS = "Get API key at myprovider.ai"
        API_KEY_ENV_VAR = "MY_API_KEY"
        DEFAULT_BASE_URL = "https://api.myprovider.ai/v1"
        DEFAULT_MODEL = "my-model"
        FALLBACK_MODELS = [...]
        MODEL_PRICING = {...}
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from bmlib.llm.data_types import LLMMessage, LLMResponse
from bmlib.llm.providers.base import (
    BaseProvider,
    ModelMetadata,
    ModelPricing,
    ProviderCapabilities,
)

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN_ESTIMATE = 4
CACHE_TTL_SECONDS = 3600


class OpenAICompatibleProvider(BaseProvider):
    """Base for providers that support the OpenAI chat completions API."""

    # --- Subclass MUST override these ---
    API_KEY_ENV_VAR: str = ""
    DEFAULT_BASE_URL: str = ""
    DEFAULT_MODEL: str = ""
    FALLBACK_MODELS: list[ModelMetadata] = []
    MODEL_PRICING: dict[str, ModelPricing] = {}

    _FALLBACK_PRICING = ModelPricing(input_cost=1.0, output_cost=3.0)

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: object,
    ) -> None:
        resolved_key = api_key or os.environ.get(self.API_KEY_ENV_VAR, "")
        resolved_url = base_url or self.DEFAULT_BASE_URL
        super().__init__(api_key=resolved_key or None, base_url=resolved_url, **kwargs)
        self._models_cache: list[ModelMetadata] | None = None
        self._cache_timestamp: float = 0.0

    # --- Properties ---

    @property
    def is_local(self) -> bool:
        return False

    @property
    def is_free(self) -> bool:
        return False

    @property
    def requires_api_key(self) -> bool:
        return True

    @property
    def api_key_env_var(self) -> str:
        return self.API_KEY_ENV_VAR

    @property
    def default_base_url(self) -> str:
        return self.DEFAULT_BASE_URL

    @property
    def default_model(self) -> str:
        return self.DEFAULT_MODEL

    # --- Client ---

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError(
                    "openai package not installed. Install with: pip install openai"
                )
            self._client = OpenAI(
                api_key=self._api_key or "unused",
                base_url=self._base_url,
            )
        return self._client

    # --- Chat ---

    def chat(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: object,
    ) -> LLMResponse:
        model = model or self.default_model
        client = self._get_client()

        top_p: float | None = kwargs.get("top_p")  # type: ignore[assignment]
        json_mode: bool = kwargs.get("json_mode", False)  # type: ignore[assignment]

        openai_messages: list[dict[str, str]] = [
            {"role": msg.role, "content": msg.content} for msg in messages
        ]

        request_kwargs: dict[str, object] = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if top_p is not None:
            request_kwargs["top_p"] = top_p
        if json_mode:
            request_kwargs["response_format"] = {"type": "json_object"}

        response = client.chat.completions.create(**request_kwargs)

        choice = response.choices[0]
        content = choice.message.content or ""

        if json_mode and content:
            try:
                json.loads(content)
            except json.JSONDecodeError:
                content = _extract_json(content)

        return LLMResponse(
            content=content,
            model=model,
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            stop_reason=choice.finish_reason,
        )

    # --- Model listing ---

    def list_models(self, force_refresh: bool = False) -> list[ModelMetadata]:
        if (
            not force_refresh
            and self._models_cache is not None
            and time.time() - self._cache_timestamp < CACHE_TTL_SECONDS
        ):
            return self._models_cache

        try:
            client = self._get_client()
            api_response = client.models.list()
            model_list = api_response.data if hasattr(api_response, "data") else []
            models = []
            for m in model_list:
                model_id = m.id
                pricing = self.MODEL_PRICING.get(model_id, self._FALLBACK_PRICING)
                models.append(
                    ModelMetadata(
                        model_id=model_id,
                        display_name=model_id,
                        context_window=128_000,
                        pricing=pricing,
                        capabilities=ProviderCapabilities(
                            supports_system_messages=True,
                            max_context_window=128_000,
                        ),
                    )
                )
            if models:
                self._models_cache = models
                self._cache_timestamp = time.time()
                return models
        except Exception as e:
            logger.warning(
                "Failed to fetch models from %s API: %s", self.DISPLAY_NAME, e
            )

        return list(self.FALLBACK_MODELS)

    # --- Connection test ---

    def test_connection(self) -> tuple[bool, str]:
        try:
            client = self._get_client()
            result = client.models.list()
            data = result.data if hasattr(result, "data") else []
            return True, f"Connected. {len(data)} models available."
        except Exception as e:
            return False, f"Connection failed: {e}"

    # --- Tokens ---

    def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // CHARS_PER_TOKEN_ESTIMATE

    def get_model_pricing(self, model: str) -> ModelPricing:
        return self.MODEL_PRICING.get(model, self._FALLBACK_PRICING)


def _extract_json(text: str) -> str:
    """Extract JSON from text that may contain markdown code blocks."""
    code_block_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL
    )
    if code_block_match:
        candidate = code_block_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        candidate = brace_match.group(0)
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return text
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_openai_compat.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/llm/providers/openai_compat.py tests/test_openai_compat.py
git commit -m "feat(llm): add OpenAI-compatible base provider class"
```

---

### Task 2: OpenAI Provider Subclass

**Files:**
- Create: `bmlib/llm/providers/openai_provider.py`

**Step 1: Create the OpenAI provider**

Create `bmlib/llm/providers/openai_provider.py`:

```python
# bmlib — shared library for biomedical literature tools
# Copyright (C) 2024-2026 Dr Horst Herb
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""OpenAI provider — GPT models via the OpenAI API."""

from __future__ import annotations

from bmlib.llm.providers.base import ModelMetadata, ModelPricing, ProviderCapabilities
from bmlib.llm.providers.openai_compat import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI GPT models."""

    PROVIDER_NAME = "openai"
    DISPLAY_NAME = "OpenAI"
    DESCRIPTION = "GPT models via OpenAI API"
    WEBSITE_URL = "https://platform.openai.com"
    SETUP_INSTRUCTIONS = "Get API key from platform.openai.com/api-keys"

    API_KEY_ENV_VAR = "OPENAI_API_KEY"
    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_MODEL = "gpt-4o"

    MODEL_PRICING = {
        "gpt-4o": ModelPricing(input_cost=2.50, output_cost=10.0),
        "gpt-4o-mini": ModelPricing(input_cost=0.15, output_cost=0.60),
        "gpt-4-turbo": ModelPricing(input_cost=10.0, output_cost=30.0),
        "o1": ModelPricing(input_cost=15.0, output_cost=60.0),
        "o1-mini": ModelPricing(input_cost=3.0, output_cost=12.0),
        "o3-mini": ModelPricing(input_cost=1.10, output_cost=4.40),
    }

    FALLBACK_MODELS = [
        ModelMetadata(
            model_id="gpt-4o",
            display_name="GPT-4o",
            context_window=128_000,
            pricing=ModelPricing(input_cost=2.50, output_cost=10.0),
            capabilities=ProviderCapabilities(
                supports_vision=True,
                supports_function_calling=True,
                supports_system_messages=True,
                max_context_window=128_000,
            ),
        ),
        ModelMetadata(
            model_id="gpt-4o-mini",
            display_name="GPT-4o Mini",
            context_window=128_000,
            pricing=ModelPricing(input_cost=0.15, output_cost=0.60),
        ),
        ModelMetadata(
            model_id="o3-mini",
            display_name="o3-mini",
            context_window=200_000,
            pricing=ModelPricing(input_cost=1.10, output_cost=4.40),
        ),
    ]
```

**Step 2: Verify import works**

Run: `cd /Users/hherb/src/bmlib && python -c "from bmlib.llm.providers.openai_provider import OpenAIProvider; print(OpenAIProvider.PROVIDER_NAME)"`
Expected: `openai`

**Step 3: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/llm/providers/openai_provider.py
git commit -m "feat(llm): add OpenAI provider"
```

---

### Task 3: DeepSeek Provider Subclass

**Files:**
- Create: `bmlib/llm/providers/deepseek.py`

**Step 1: Create the DeepSeek provider**

Create `bmlib/llm/providers/deepseek.py`:

```python
# bmlib — shared library for biomedical literature tools
# Copyright (C) 2024-2026 Dr Horst Herb
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""DeepSeek provider — DeepSeek models via OpenAI-compatible API."""

from __future__ import annotations

from bmlib.llm.providers.base import ModelMetadata, ModelPricing
from bmlib.llm.providers.openai_compat import OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek models via OpenAI-compatible API."""

    PROVIDER_NAME = "deepseek"
    DISPLAY_NAME = "DeepSeek"
    DESCRIPTION = "DeepSeek models (DeepSeek-V3, R1)"
    WEBSITE_URL = "https://platform.deepseek.com"
    SETUP_INSTRUCTIONS = "Get API key from platform.deepseek.com/api_keys"

    API_KEY_ENV_VAR = "DEEPSEEK_API_KEY"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-chat"

    MODEL_PRICING = {
        "deepseek-chat": ModelPricing(input_cost=0.27, output_cost=1.10),
        "deepseek-reasoner": ModelPricing(input_cost=0.55, output_cost=2.19),
    }

    FALLBACK_MODELS = [
        ModelMetadata(
            model_id="deepseek-chat",
            display_name="DeepSeek-V3 (Chat)",
            context_window=64_000,
            pricing=ModelPricing(input_cost=0.27, output_cost=1.10),
        ),
        ModelMetadata(
            model_id="deepseek-reasoner",
            display_name="DeepSeek-R1 (Reasoner)",
            context_window=64_000,
            pricing=ModelPricing(input_cost=0.55, output_cost=2.19),
        ),
    ]
```

**Step 2: Verify import works**

Run: `cd /Users/hherb/src/bmlib && python -c "from bmlib.llm.providers.deepseek import DeepSeekProvider; print(DeepSeekProvider.PROVIDER_NAME)"`
Expected: `deepseek`

**Step 3: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/llm/providers/deepseek.py
git commit -m "feat(llm): add DeepSeek provider"
```

---

### Task 4: Mistral Provider Subclass

**Files:**
- Create: `bmlib/llm/providers/mistral.py`

**Step 1: Create the Mistral provider**

Create `bmlib/llm/providers/mistral.py`:

```python
# bmlib — shared library for biomedical literature tools
# Copyright (C) 2024-2026 Dr Horst Herb
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Mistral AI provider — Mistral models via OpenAI-compatible API."""

from __future__ import annotations

from bmlib.llm.providers.base import ModelMetadata, ModelPricing, ProviderCapabilities
from bmlib.llm.providers.openai_compat import OpenAICompatibleProvider


class MistralProvider(OpenAICompatibleProvider):
    """Mistral AI models via OpenAI-compatible API."""

    PROVIDER_NAME = "mistral"
    DISPLAY_NAME = "Mistral AI"
    DESCRIPTION = "Mistral models (Large, Small, Codestral)"
    WEBSITE_URL = "https://console.mistral.ai"
    SETUP_INSTRUCTIONS = "Get API key from console.mistral.ai/api-keys"

    API_KEY_ENV_VAR = "MISTRAL_API_KEY"
    DEFAULT_BASE_URL = "https://api.mistral.ai/v1"
    DEFAULT_MODEL = "mistral-large-latest"

    MODEL_PRICING = {
        "mistral-large-latest": ModelPricing(input_cost=2.0, output_cost=6.0),
        "mistral-small-latest": ModelPricing(input_cost=0.1, output_cost=0.3),
        "codestral-latest": ModelPricing(input_cost=0.3, output_cost=0.9),
        "ministral-8b-latest": ModelPricing(input_cost=0.1, output_cost=0.1),
        "pixtral-large-latest": ModelPricing(input_cost=2.0, output_cost=6.0),
    }

    FALLBACK_MODELS = [
        ModelMetadata(
            model_id="mistral-large-latest",
            display_name="Mistral Large",
            context_window=128_000,
            pricing=ModelPricing(input_cost=2.0, output_cost=6.0),
            capabilities=ProviderCapabilities(
                supports_vision=True,
                supports_function_calling=True,
                supports_system_messages=True,
                max_context_window=128_000,
            ),
        ),
        ModelMetadata(
            model_id="mistral-small-latest",
            display_name="Mistral Small",
            context_window=128_000,
            pricing=ModelPricing(input_cost=0.1, output_cost=0.3),
        ),
        ModelMetadata(
            model_id="codestral-latest",
            display_name="Codestral",
            context_window=256_000,
            pricing=ModelPricing(input_cost=0.3, output_cost=0.9),
        ),
    ]
```

**Step 2: Verify import works**

Run: `cd /Users/hherb/src/bmlib && python -c "from bmlib.llm.providers.mistral import MistralProvider; print(MistralProvider.PROVIDER_NAME)"`
Expected: `mistral`

**Step 3: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/llm/providers/mistral.py
git commit -m "feat(llm): add Mistral AI provider"
```

---

### Task 5: Gemini Provider Subclass

**Files:**
- Create: `bmlib/llm/providers/gemini.py`

**Step 1: Create the Gemini provider**

Create `bmlib/llm/providers/gemini.py`:

```python
# bmlib — shared library for biomedical literature tools
# Copyright (C) 2024-2026 Dr Horst Herb
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Google Gemini provider — Gemini models via OpenAI-compatible API."""

from __future__ import annotations

from bmlib.llm.providers.base import ModelMetadata, ModelPricing, ProviderCapabilities
from bmlib.llm.providers.openai_compat import OpenAICompatibleProvider


class GeminiProvider(OpenAICompatibleProvider):
    """Google Gemini models via OpenAI-compatible API."""

    PROVIDER_NAME = "gemini"
    DISPLAY_NAME = "Google Gemini"
    DESCRIPTION = "Gemini models via Google AI Studio"
    WEBSITE_URL = "https://aistudio.google.com"
    SETUP_INSTRUCTIONS = "Get API key from aistudio.google.com/apikey"

    API_KEY_ENV_VAR = "GEMINI_API_KEY"
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
    DEFAULT_MODEL = "gemini-2.0-flash"

    MODEL_PRICING = {
        "gemini-2.0-flash": ModelPricing(input_cost=0.10, output_cost=0.40),
        "gemini-2.0-flash-lite": ModelPricing(input_cost=0.0, output_cost=0.0),
        "gemini-1.5-pro": ModelPricing(input_cost=1.25, output_cost=5.0),
        "gemini-1.5-flash": ModelPricing(input_cost=0.075, output_cost=0.30),
        "gemini-2.5-pro-preview-05-06": ModelPricing(input_cost=1.25, output_cost=10.0),
        "gemini-2.5-flash-preview-05-20": ModelPricing(input_cost=0.15, output_cost=0.60),
    }

    FALLBACK_MODELS = [
        ModelMetadata(
            model_id="gemini-2.0-flash",
            display_name="Gemini 2.0 Flash",
            context_window=1_000_000,
            pricing=ModelPricing(input_cost=0.10, output_cost=0.40),
            capabilities=ProviderCapabilities(
                supports_vision=True,
                supports_function_calling=True,
                supports_system_messages=True,
                max_context_window=1_000_000,
            ),
        ),
        ModelMetadata(
            model_id="gemini-1.5-pro",
            display_name="Gemini 1.5 Pro",
            context_window=2_000_000,
            pricing=ModelPricing(input_cost=1.25, output_cost=5.0),
        ),
        ModelMetadata(
            model_id="gemini-2.5-pro-preview-05-06",
            display_name="Gemini 2.5 Pro Preview",
            context_window=1_000_000,
            pricing=ModelPricing(input_cost=1.25, output_cost=10.0),
        ),
        ModelMetadata(
            model_id="gemini-2.5-flash-preview-05-20",
            display_name="Gemini 2.5 Flash Preview",
            context_window=1_000_000,
            pricing=ModelPricing(input_cost=0.15, output_cost=0.60),
        ),
    ]
```

**Step 2: Verify import works**

Run: `cd /Users/hherb/src/bmlib && python -c "from bmlib.llm.providers.gemini import GeminiProvider; print(GeminiProvider.PROVIDER_NAME)"`
Expected: `gemini`

**Step 3: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/llm/providers/gemini.py
git commit -m "feat(llm): add Google Gemini provider"
```

---

### Task 6: Register New Providers in Registry

**Files:**
- Modify: `bmlib/llm/providers/__init__.py:72-89`

**Step 1: Run existing registry test to confirm baseline**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_llm.py::TestProviderRegistry -v`
Expected: PASS

**Step 2: Update `_ensure_builtins` to register all 6 providers**

In `bmlib/llm/providers/__init__.py`, replace the `_ensure_builtins` function (lines 72-89) with:

```python
def _ensure_builtins() -> None:
    """Lazily register built-in providers on first access."""
    if _REGISTRY:
        return

    # Anthropic
    try:
        from bmlib.llm.providers.anthropic import AnthropicProvider
        _REGISTRY["anthropic"] = AnthropicProvider
    except ImportError:
        pass

    # Ollama
    try:
        from bmlib.llm.providers.ollama import OllamaProvider
        _REGISTRY["ollama"] = OllamaProvider
    except ImportError:
        pass

    # OpenAI
    try:
        from bmlib.llm.providers.openai_provider import OpenAIProvider
        _REGISTRY["openai"] = OpenAIProvider
    except ImportError:
        pass

    # DeepSeek
    try:
        from bmlib.llm.providers.deepseek import DeepSeekProvider
        _REGISTRY["deepseek"] = DeepSeekProvider
    except ImportError:
        pass

    # Mistral
    try:
        from bmlib.llm.providers.mistral import MistralProvider
        _REGISTRY["mistral"] = MistralProvider
    except ImportError:
        pass

    # Gemini
    try:
        from bmlib.llm.providers.gemini import GeminiProvider
        _REGISTRY["gemini"] = GeminiProvider
    except ImportError:
        pass
```

**Step 3: Run registry tests again**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_llm.py::TestProviderRegistry -v`
Expected: PASS

**Step 4: Run the full test suite**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_llm.py tests/test_openai_compat.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/llm/providers/__init__.py
git commit -m "feat(llm): register OpenAI, DeepSeek, Mistral, Gemini providers"
```

---

### Task 7: Generalize LLMClient Provider Config

**Files:**
- Modify: `bmlib/llm/client.py:60-71`

**Step 1: Update `LLMClient.__init__` to accept generic provider config**

In `bmlib/llm/client.py`, replace the `__init__` method (lines 60-71) with:

```python
    def __init__(
        self,
        default_provider: str = DEFAULT_PROVIDER,
        ollama_host: str | None = None,
        anthropic_api_key: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.default_provider = default_provider
        self._provider_config: dict[str, dict[str, object]] = {
            "anthropic": {"api_key": anthropic_api_key or api_key},
            "ollama": {"base_url": ollama_host},
        }
        # For OpenAI-compatible providers, pass api_key and base_url
        for name in ("openai", "deepseek", "mistral", "gemini"):
            self._provider_config[name] = {"api_key": api_key, "base_url": base_url}
        self._providers: dict[str, BaseProvider] = {}
```

**Step 2: Run existing tests**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/test_llm.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
cd /Users/hherb/src/bmlib
git add bmlib/llm/client.py
git commit -m "feat(llm): generalize LLMClient to pass config to all providers"
```

---

### Task 8: Update BioMedicalNews Config

**Files:**
- Modify: `bmnews/config.py:106-113` (LLMConfig dataclass)
- Modify: `bmnews/config.py:299-306` (DEFAULT_CONFIG_TOML llm section)

**Step 1: Run existing config tests**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_config.py -v`
Expected: All PASS

**Step 2: Add `api_key` and `base_url` fields to `LLMConfig`**

In `bmnews/config.py`, replace lines 106-113 with:

```python
@dataclass
class LLMConfig:
    provider: str = "ollama"
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    ollama_host: str = ""
    anthropic_api_key: str = ""
    api_key: str = ""
    base_url: str = ""
    concurrency: int = 1
```

**Step 3: Update DEFAULT_CONFIG_TOML**

In `bmnews/config.py`, update the `[llm]` section of `DEFAULT_CONFIG_TOML` (lines 299-306) to:

```toml
[llm]
provider = "ollama"
# model = "ollama:medgemma4B_it_q8"
temperature = 0.3
max_tokens = 4096
# ollama_host = "http://localhost:11434"
# anthropic_api_key = ""
# api_key = ""
# base_url = ""
concurrency = 1
```

**Step 4: Update `build_llm_client` in `bmnews/pipeline.py`**

In `bmnews/pipeline.py`, replace lines 49-55 with:

```python
def build_llm_client(config: AppConfig) -> LLMClient:
    """Build an LLM client from config."""
    return LLMClient(
        default_provider=config.llm.provider,
        ollama_host=config.llm.ollama_host or None,
        anthropic_api_key=config.llm.anthropic_api_key or None,
        api_key=config.llm.api_key or None,
        base_url=config.llm.base_url or None,
    )
```

**Step 5: Run config tests**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_config.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add bmnews/config.py bmnews/pipeline.py
git commit -m "feat(config): add api_key and base_url to LLMConfig, update pipeline"
```

---

### Task 9: Add Models API Endpoint with Cache

**Files:**
- Modify: `bmnews/gui/routes/settings.py`

**Step 1: Add the `/settings/models` endpoint**

In `bmnews/gui/routes/settings.py`, add at the top of the file (after existing imports, around line 13):

```python
import json
from pathlib import Path as _Path

from bmlib.llm import LLMClient
```

Then add the following route after the `save_settings` function (after line 80):

```python
_MODEL_CACHE_PATH = _Path("~/.bmnews/model_cache.json").expanduser()


def _load_model_cache() -> dict:
    if _MODEL_CACHE_PATH.exists():
        try:
            return json.loads(_MODEL_CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_model_cache(cache: dict) -> None:
    _MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MODEL_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


@settings_bp.route("/settings/models")
def list_models():
    """Return available models for a provider as JSON or HTML options."""
    provider = request.args.get("provider", "ollama")
    refresh = request.args.get("refresh", "") == "1"
    config: AppConfig = current_app.config["BMNEWS_CONFIG"]

    cache = _load_model_cache()

    if not refresh and provider in cache:
        models = cache[provider]
    else:
        try:
            client = LLMClient(
                default_provider=provider,
                ollama_host=config.llm.ollama_host or None,
                anthropic_api_key=config.llm.anthropic_api_key or None,
                api_key=config.llm.api_key or None,
                base_url=config.llm.base_url or None,
            )
            raw = client.list_models(provider=provider)
            models = [{"id": m, "name": m} if isinstance(m, str) else {"id": m, "name": m} for m in raw]
        except Exception:
            models = []

        if models:
            cache[provider] = models
            _save_model_cache(cache)

    # Return as HTML datalist options for HTMX consumption
    current_model = config.llm.model
    options_html = ""
    for m in models:
        mid = m if isinstance(m, str) else m.get("id", "")
        options_html += f'<option value="{mid}">'
    return options_html
```

**Step 2: Run GUI tests**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_gui_app.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add bmnews/gui/routes/settings.py
git commit -m "feat(gui): add /settings/models endpoint with persistent cache"
```

---

### Task 10: Update Settings UI — Provider Dropdown + Model Datalist

**Files:**
- Modify: `bmnews/gui/templates/fragments/settings.html:34-46`

**Step 1: Replace the LLM section in settings.html**

In `bmnews/gui/templates/fragments/settings.html`, replace lines 34-46 (the `<section class="settings-section">` for LLM) with:

```html
                <section class="settings-section">
                    <h2>LLM</h2>
                    <label>Provider:
                        <select name="llm.provider" id="llm-provider"
                                hx-get="/settings/models"
                                hx-target="#model-options"
                                hx-trigger="change"
                                hx-include="[name='llm.provider']"
                                hx-vals='{"provider": ""}'
                                onchange="this.setAttribute('hx-vals', JSON.stringify({provider: this.value}))">
                            <option value="ollama" {% if config.llm.provider == "ollama" %}selected{% endif %}>Ollama</option>
                            <option value="anthropic" {% if config.llm.provider == "anthropic" %}selected{% endif %}>Anthropic</option>
                            <option value="openai" {% if config.llm.provider == "openai" %}selected{% endif %}>OpenAI</option>
                            <option value="deepseek" {% if config.llm.provider == "deepseek" %}selected{% endif %}>DeepSeek</option>
                            <option value="mistral" {% if config.llm.provider == "mistral" %}selected{% endif %}>Mistral AI</option>
                            <option value="gemini" {% if config.llm.provider == "gemini" %}selected{% endif %}>Google Gemini</option>
                        </select>
                    </label>
                    <label>Model:
                        <span style="display: flex; gap: 0.25rem; align-items: center;">
                            <input type="text" name="llm.model" id="llm-model"
                                   value="{{ config.llm.model }}" list="model-options"
                                   placeholder="Select or type a model name">
                            <datalist id="model-options"
                                      hx-get="/settings/models?provider={{ config.llm.provider }}"
                                      hx-trigger="load"
                                      hx-swap="innerHTML">
                            </datalist>
                            <button type="button" class="btn btn-sm"
                                    title="Refresh model list from provider"
                                    onclick="htmx.ajax('GET', '/settings/models?provider=' + document.getElementById('llm-provider').value + '&refresh=1', {target: '#model-options', swap: 'innerHTML'})">
                                &#x21bb;
                            </button>
                        </span>
                    </label>
                    <label>API Key:
                        <input type="password" name="llm.api_key"
                               value="{{ config.llm.api_key }}"
                               placeholder="Provider API key (or set env var)">
                    </label>
                    <label>Concurrency:
                        <input type="number" name="llm.concurrency" value="{{ config.llm.concurrency }}" min="1" max="10">
                    </label>
                </section>
```

**Step 2: Fix the HTMX provider change trigger**

The provider `<select>` needs a simpler HTMX approach. Replace the `hx-vals` / `onchange` pattern with a direct approach — use `hx-get` with a JS expression. Actually, HTMX can use `hx-vals` with `js:` prefix. Update the select to:

```html
                        <select name="llm.provider" id="llm-provider"
                                hx-get="/settings/models"
                                hx-target="#model-options"
                                hx-trigger="change"
                                hx-vals="js:{provider: document.getElementById('llm-provider').value}">
```

**Step 3: Verify the UI loads**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/test_gui_app.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
cd /Users/hherb/src/BioMedicalNews
git add bmnews/gui/templates/fragments/settings.html
git commit -m "feat(gui): auto-populated model selector with provider dropdown"
```

---

### Task 11: Run Full Test Suites in Both Projects

**Step 1: Run bmlib tests**

Run: `cd /Users/hherb/src/bmlib && python -m pytest tests/ -v`
Expected: All PASS

**Step 2: Run BioMedicalNews tests**

Run: `cd /Users/hherb/src/BioMedicalNews && python -m pytest tests/ -v`
Expected: All PASS

**Step 3: If any failures, fix them before proceeding**

---

### Task 12: Install openai dependency

**Step 1: Check if openai is already installed**

Run: `cd /Users/hherb/src/bmlib && pip show openai`

If not installed:

Run: `cd /Users/hherb/src/bmlib && pip install openai`

**Step 2: Check pyproject.toml or setup.cfg for dependency declaration**

If there's a `pyproject.toml`, add `openai` to the optional dependencies (e.g. under an `[openai]` extra or in the main deps list).

**Step 3: Commit dependency changes if any**
