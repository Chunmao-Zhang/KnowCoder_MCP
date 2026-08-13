"""Process-local model selection layered over the protected Harness config."""

from __future__ import annotations

import os
from typing import Any


FRIDAY_DEFAULT_BASE_URL = "https://aigc.sankuai.com/v1/openai/native"
FRIDAY_MODELS = {
    "deepseek-v4-pro-baidu": {
        "context_window": 128_000,
        "max_tokens": 16_384,
    },
    "deepseek-v4-flash-baidu": {
        "context_window": 128_000,
        "max_tokens": 16_384,
    },
}
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODELS = {
    "deepseek-v4-pro": {
        "context_window": 131_072,
        "max_tokens": 16_384,
    },
    "deepseek-v4-flash": {
        "context_window": 131_072,
        "max_tokens": 16_384,
    },
    "deepseek-chat": {
        "context_window": 131_072,
        "max_tokens": 16_384,
    },
}
KNOWCODER_MODELS = {
    "deepseek-v4-flash": {"context_window": 131_072, "max_tokens": 16_384},
    "deepseek-v4-pro": {"context_window": 131_072, "max_tokens": 16_384},
}


def selected_model_ref() -> str:
    value = os.environ.get("SCHEMA_AGENT_MODEL", "").strip()
    if not value:
        raise ValueError("Missing SCHEMA_AGENT_MODEL. Set it in the project .env file.")
    if "/" not in value:
        raise ValueError("SCHEMA_AGENT_MODEL must use provider/model_id format")
    return value


def apply_runtime_model_override(config: Any) -> Any:
    """Override only model transport fields; keep the protected Harness workflow."""
    model_ref = selected_model_ref()
    provider_name, model_id = model_ref.split("/", 1)
    provider = config.providers.get(provider_name)
    if provider is not None:
        base_url = provider.base_url
        api_key = os.environ.get(f"{provider_name.upper()}_API_KEY", "") or provider.api_key
        model_settings = dict(provider.models.get(model_id, {}))
        # Prefer official DeepSeek endpoint + env key over any residual local override.
        if provider_name == "deepseek":
            base_url = os.environ.get("DEEPSEEK_BASE_URL", base_url or DEEPSEEK_DEFAULT_BASE_URL).strip()
            api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or api_key
            if not model_settings:
                model_settings = dict(DEEPSEEK_MODELS.get(model_id, {}))
    elif provider_name == "deepseek":
        base_url = os.environ.get("DEEPSEEK_BASE_URL", DEEPSEEK_DEFAULT_BASE_URL).strip()
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        model_settings = dict(DEEPSEEK_MODELS.get(model_id, {}))
    elif provider_name == "friday":
        base_url = os.environ.get("FRIDAY_BASE_URL", FRIDAY_DEFAULT_BASE_URL).strip()
        api_key = os.environ.get("FRIDAY_API_KEY", "").strip()
        model_settings = dict(FRIDAY_MODELS.get(model_id, {}))
    elif provider_name == "knowcoder":
        base_url = os.environ.get("KNOWCODER_BASE_URL", "").strip()
        api_key = os.environ.get("KNOWCODER_API_KEY", "").strip()
        model_settings = dict(KNOWCODER_MODELS.get(model_id, {"context_window": 131_072, "max_tokens": 16_384}))
    else:
        raise ValueError(f"Unknown runtime model provider: {provider_name}")
    if not base_url:
        raise ValueError(f"Missing base URL for provider '{provider_name}'")
    if not api_key:
        raise ValueError(f"Missing API key for provider '{provider_name}'. Set {provider_name.upper()}_API_KEY.")
    if not model_settings:
        raise ValueError(f"Unknown model '{model_id}' for provider '{provider_name}'")

    model = config.defaults.model
    model.provider = provider_name
    model.model_id = model_id
    model.base_url = base_url
    model.api_key = api_key
    model.temperature = 0.0
    model.max_tokens = int(model_settings.get("max_tokens", model.max_tokens))
    model.context_window = int(model_settings.get("context_window", model.context_window))
    model.response_format = model_settings.get("response_format")
    return config
