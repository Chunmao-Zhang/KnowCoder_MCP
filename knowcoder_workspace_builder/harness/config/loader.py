"""配置加载器

读取 harness.json -> HarnessConfig。
支持 ${ENV_VAR} 环境变量替换。
支持 "provider/model_id" 简写引用顶层 providers 配置。
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.harness.config.schema import (
    AgentConfig,
    ContextConfig,
    DefaultsConfig,
    HarnessConfig,
    MCPServerConfig,
    MiddlewareConfig,
    ModelConfig,
    ProfileConfig,
    ProviderConfig,
    ToolsConfig,
)


def load_env_file(path: str | Path, *, override: bool = False) -> None:
    """Load a small POSIX-style .env file if it exists.

    The project intentionally avoids requiring python-dotenv. This parser covers
    the common KEY=value / export KEY=value forms used by the local launchers.
    Existing environment variables win by default.
    """

    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if not override and os.environ.get(key):
            continue
        value = value.strip()
        if value:
            try:
                parts = shlex.split(value, posix=True)
                value = parts[0] if parts else ""
            except ValueError:
                value = value.strip('"').strip("'")
        os.environ[key] = value


def _resolve_env(value: str) -> str:
    """替换 ${ENV_VAR} 占位符为环境变量值。未解析的占位符返回空串，避免把
    形如 "${DEEPSEEK_API_KEY}" 的字面量当成真实密钥使用。"""
    if not isinstance(value, str):
        return value
    resolved = re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), ""),
        value,
    )
    return resolved


def load_project_env(config_path: str | Path) -> None:
    """Load project-local environment configuration for a harness config file.

    Runtime keys may live next to this harness config, or in the current target
    project directory when Builder is installed elsewhere. Exported shell
    variables always take highest precedence.
    """

    config_dir = Path(config_path).resolve().parent
    roots: list[Path] = []
    for root in (Path.cwd().resolve(), config_dir, config_dir.parent):
        if root not in roots:
            roots.append(root)

    # Existing process variables have highest priority. Project-local files are
    # considered from most specific to least specific, and examples only fill
    # keys that remain unset.
    for root in roots:
        load_env_file(root / ".env")
    for root in roots:
        load_env_file(root / ".env.example")


def _parse_providers(data: dict[str, Any] | None) -> dict[str, ProviderConfig]:
    """解析顶层 providers 配置"""
    if not data:
        return {}
    providers = {}
    for name, cfg in data.items():
        # An explicit `<PROVIDER>_API_KEY` environment variable always wins over
        # whatever is in the file, so secrets never have to live in the repo.
        env_key = os.environ.get(f"{name.upper()}_API_KEY", "")
        providers[name] = ProviderConfig(
            base_url=_resolve_env(cfg.get("base_url", "")),
            api_key=env_key or _resolve_env(cfg.get("api_key", "")),
            models=cfg.get("models", {}),
        )
    return providers


def _resolve_model_ref(
    model_value: Any,
    providers: dict[str, ProviderConfig],
    temperature: float = 0.0,
) -> ModelConfig | None:
    """解析 model 字段

    支持两种格式：
    - 字符串简写: "provider/model_id" → 从 providers 中查找
    - 完整 dict: { "provider": ..., "model_id": ..., ... } → 直接解析
    - None → 返回 None（继承 defaults）
    """
    if model_value is None:
        return None

    if isinstance(model_value, str):
        # 简写格式: "siliconflow/Qwen/Qwen3.5-27B"
        # 第一个 / 之前是 provider name，之后是 model_id
        parts = model_value.split("/", 1)
        if len(parts) != 2:
            return None

        provider_name, model_id = parts
        provider = providers.get(provider_name)
        if not provider:
            return ModelConfig(
                provider=provider_name,
                model_id=model_id,
                temperature=temperature,
            )

        # 从 provider 中获取模型参数
        model_params = provider.models.get(model_id, {})
        return ModelConfig(
            provider=provider_name,
            model_id=model_id,
            base_url=provider.base_url,
            api_key=provider.api_key,
            temperature=temperature,
            max_tokens=model_params.get("max_tokens", 16384),
            context_window=model_params.get("context_window", 128000),
            response_format=model_params.get("response_format"),
            extra_body=model_params.get("extra_body"),
        )

    if isinstance(model_value, dict):
        # 完整 dict 格式（向后兼容）
        return _parse_model_dict(model_value)

    return None


def _parse_model_dict(data: dict[str, Any] | None) -> ModelConfig | None:
    """解析完整的 model dict（向后兼容）"""
    if not data:
        return None
    return ModelConfig(
        provider=data.get("provider", ""),
        model_id=data.get("model_id", ""),
        base_url=_resolve_env(data.get("base_url", "")),
        api_key=_resolve_env(data.get("api_key", "")),
        temperature=data.get("temperature", 0.0),
        max_tokens=data.get("max_tokens", 16384),
        context_window=data.get("context_window", 128000),
        response_format=data.get("response_format"),
        extra_body=data.get("extra_body"),
    )


def _parse_tools(data: dict[str, Any] | None) -> ToolsConfig | None:
    if not data:
        return None
    return ToolsConfig(
        allow=data.get("allow", ["*"]),
        deny=data.get("deny", []),
    )


def _parse_context(data: dict[str, Any] | None) -> ContextConfig | None:
    if not data:
        return None
    return ContextConfig(
        keep_turns=data.get("keep_turns", 4),
        max_input_tokens=data.get("max_input_tokens", 128000),
        summary_trigger_fraction=data.get("summary_trigger_fraction", 0.85),
        offload_threshold=data.get("offload_threshold", 20000),
    )


def _parse_middleware(data: list[Any] | None) -> list[MiddlewareConfig]:
    if not data:
        return []
    result: list[MiddlewareConfig] = []
    for item in data:
        if isinstance(item, str):
            result.append(MiddlewareConfig(type=item))
        elif isinstance(item, dict):
            item_type = item.get("type") or item.get("name") or item.get("path") or ""
            args = dict(item.get("args", {}))
            for key, value in item.items():
                if key not in {"type", "name", "path", "args"}:
                    args[key] = value
            result.append(MiddlewareConfig(type=item_type, args=args))
    return result


def _parse_profile(data: dict[str, Any] | None) -> ProfileConfig | None:
    if not data:
        return None
    general = data.get("general_purpose_subagent")
    enabled = None
    if isinstance(general, bool):
        enabled = general
    elif isinstance(general, dict) and "enabled" in general:
        enabled = bool(general.get("enabled"))
    return ProfileConfig(
        general_purpose_subagent_enabled=enabled,
        excluded_tools=list(data.get("excluded_tools", [])),
        excluded_middleware=list(data.get("excluded_middleware", [])),
    )


def _parse_mcp_servers(data: Any) -> dict[str, MCPServerConfig]:
    if not isinstance(data, dict):
        return {}
    servers: dict[str, MCPServerConfig] = {}
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        env = cfg.get("env", {})
        resolved_env = {
            str(key): _resolve_env(str(value))
            for key, value in env.items()
        } if isinstance(env, dict) else {}
        args = cfg.get("args", [])
        servers[str(name)] = MCPServerConfig(
            transport=str(cfg.get("transport", "stdio") or "stdio"),
            command=str(cfg.get("command", "") or ""),
            args=[str(item) for item in args] if isinstance(args, list) else [],
            cwd=str(cfg.get("cwd", "") or ""),
            env=resolved_env,
            enabled=bool(cfg.get("enabled", True)),
        )
    return servers


def _parse_agent(data: dict[str, Any], providers: dict[str, ProviderConfig], temperature: float) -> AgentConfig:
    """解析单个 agent 配置"""
    return AgentConfig(
        id=data["id"],
        name=data.get("name", data["id"]),
        workspace=data.get("workspace", f"workspaces/{data['id']}"),
        model=_resolve_model_ref(data.get("model"), providers, temperature),
        skills=data.get("skills", []),
        subagents=data.get("subagents", []),
        tools=_parse_tools(data.get("tools")),
        context=_parse_context(data.get("context")),
        max_steps=data.get("max_steps"),
        description=data.get("description", ""),
        prompt=data.get("prompt"),  # None = 继承 defaults
        default=data.get("default", False),
        middleware=_parse_middleware(data.get("middleware")),
        profile=_parse_profile(data.get("profile")),
    )


def load_config(config_path: str | Path) -> HarnessConfig:
    """加载 harness.json 并解析为 HarnessConfig"""
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    load_project_env(config_path)

    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # providers（顶层）
    providers = _parse_providers(raw.get("providers"))

    # defaults
    d = raw.get("defaults", {})
    temperature = d.get("temperature", 0.0)
    default_model = _resolve_model_ref(d.get("model"), providers, temperature) or ModelConfig()

    defaults = DefaultsConfig(
        model=default_model,
        tools=_parse_tools(d.get("tools")) or ToolsConfig(),
        context=_parse_context(d.get("context")) or ContextConfig(),
        max_steps=d.get("max_steps") if d.get("max_steps") not in (None, 0) else None,
        prompt=d.get("prompt", ""),
        middleware=_parse_middleware(d.get("middleware")),
        profile=_parse_profile(d.get("profile")) or ProfileConfig(),
    )

    # agents
    agents = [_parse_agent(a, providers, temperature) for a in raw.get("agents", [])]

    return HarnessConfig(
        providers=providers,
        defaults=defaults,
        agents=agents,
        mcp_servers=_parse_mcp_servers(raw.get("mcp_servers")),
    )
