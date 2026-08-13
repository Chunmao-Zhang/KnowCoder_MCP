"""配置 Schema 定义

用 dataclass 定义 harness.json 的完整数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelConfig:
    """模型配置"""
    provider: str = ""
    model_id: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.0
    max_tokens: int = 16384
    context_window: int = 128000
    response_format: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None


@dataclass
class ToolsConfig:
    """工具白黑名单"""
    allow: list[str] = field(default_factory=lambda: ["*"])
    deny: list[str] = field(default_factory=list)


@dataclass
class ContextConfig:
    """上下文管理配置"""
    keep_turns: int = 4
    max_input_tokens: int = 128000
    summary_trigger_fraction: float = 0.85
    offload_threshold: int = 20000


@dataclass
class MiddlewareConfig:
    """Config-driven middleware entry."""
    type: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProfileConfig:
    """DeepAgents harness profile config."""
    general_purpose_subagent_enabled: bool | None = None
    excluded_tools: list[str] = field(default_factory=list)
    excluded_middleware: list[str] = field(default_factory=list)


@dataclass
class MCPServerConfig:
    """MCP server connection config."""
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class DefaultsConfig:
    """全局默认配置"""
    model: ModelConfig = field(default_factory=ModelConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    max_steps: int | None = None
    prompt: str = ""  # 全局默认 prompt 文件路径，非空时替代 DeepAgents 默认 prompt
    middleware: list[MiddlewareConfig] = field(default_factory=list)
    profile: ProfileConfig = field(default_factory=ProfileConfig)


@dataclass
class AgentConfig:
    """单个 Agent 的配置"""
    id: str = ""
    name: str = ""
    workspace: str = ""
    model: ModelConfig | None = None  # None 表示继承 defaults
    skills: list[str] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    tools: ToolsConfig | None = None  # None 表示继承 defaults
    context: ContextConfig | None = None
    max_steps: int | None = None
    description: str = ""
    prompt: str | None = None  # None 表示继承 defaults，"" 表示使用 DeepAgents 默认 prompt
    default: bool = False  # 是否为默认 agent
    middleware: list[MiddlewareConfig] = field(default_factory=list)
    profile: ProfileConfig | None = None


@dataclass
class ProviderConfig:
    """单个 Provider 的配置"""
    base_url: str = ""
    api_key: str = ""
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    # models: { "model_id": { "context_window": ..., "max_tokens": ... } }


@dataclass
class HarnessConfig:
    """顶层配置，对应 harness.json"""
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    agents: list[AgentConfig] = field(default_factory=list)
    mcp_servers: dict[str, MCPServerConfig] = field(default_factory=dict)
