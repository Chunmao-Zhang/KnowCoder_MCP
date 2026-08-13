"""Agent 注册表

职责：
- 按 ID 索引 agent 配置
- 解析 subagent 依赖关系
- 配置继承：agent 未指定的字段继承 defaults
- 验证配置完整性
"""

from __future__ import annotations

from knowcoder_workspace_builder.harness.config.schema import (
    AgentConfig,
    DefaultsConfig,
    HarnessConfig,
    ModelConfig,
    ProfileConfig,
)


def _resolve_agent(agent: AgentConfig, defaults: DefaultsConfig) -> AgentConfig:
    """将 agent 中为 None 的字段用 defaults 填充，返回新对象"""
    return AgentConfig(
        id=agent.id,
        name=agent.name,
        workspace=agent.workspace,
        model=_merge_model(defaults.model, agent.model),
        skills=agent.skills,
        subagents=agent.subagents,
        tools=agent.tools if agent.tools is not None else defaults.tools,
        context=agent.context if agent.context is not None else defaults.context,
        max_steps=agent.max_steps if agent.max_steps is not None else defaults.max_steps,
        description=agent.description,
        prompt=agent.prompt if agent.prompt is not None else defaults.prompt,
        default=agent.default,
        middleware=[*defaults.middleware, *agent.middleware],
        profile=_merge_profile(defaults.profile, agent.profile),
    )


def _merge_model(base: ModelConfig, override: ModelConfig | None) -> ModelConfig:
    if override is None:
        return base
    return ModelConfig(
        provider=override.provider or base.provider,
        model_id=override.model_id or base.model_id,
        base_url=override.base_url or base.base_url,
        api_key=override.api_key or base.api_key,
        temperature=override.temperature,
        max_tokens=override.max_tokens or base.max_tokens,
        context_window=override.context_window or base.context_window,
        response_format=override.response_format if override.response_format is not None else base.response_format,
        extra_body=override.extra_body if override.extra_body is not None else base.extra_body,
    )


def _merge_profile(base: ProfileConfig, override: ProfileConfig | None) -> ProfileConfig:
    if override is None:
        return base
    enabled = (
        override.general_purpose_subagent_enabled
        if override.general_purpose_subagent_enabled is not None
        else base.general_purpose_subagent_enabled
    )
    return ProfileConfig(
        general_purpose_subagent_enabled=enabled,
        excluded_tools=[*base.excluded_tools, *override.excluded_tools],
        excluded_middleware=[*base.excluded_middleware, *override.excluded_middleware],
    )


class AgentRegistry:
    """Agent 注册表"""

    def __init__(self, config: HarnessConfig):
        self._config = config
        self._agents: dict[str, AgentConfig] = {}
        self._build()

    def _build(self) -> None:
        """构建索引，同时做配置继承"""
        for agent in self._config.agents:
            if agent.id in self._agents:
                raise ValueError(f"Duplicate agent ID: {agent.id}")
            self._agents[agent.id] = _resolve_agent(agent, self._config.defaults)

    def get(self, agent_id: str) -> AgentConfig:
        """按 ID 获取 agent 配置（已继承 defaults）"""
        if agent_id not in self._agents:
            raise KeyError(f"Agent not found: {agent_id}")
        return self._agents[agent_id]

    def get_default(self) -> AgentConfig | None:
        """获取标记为 default=True 的 agent，没有则返回 None"""
        for agent in self._agents.values():
            if agent.default:
                return agent
        return None

    def list_all(self) -> list[AgentConfig]:
        return list(self._agents.values())

    def validate(self) -> list[str]:
        """验证配置完整性，返回错误列表（空=通过）"""
        errors = []
        for agent in self._agents.values():
            for sub_id in agent.subagents:
                if sub_id not in self._agents:
                    errors.append(
                        f"Agent '{agent.id}' references unknown subagent '{sub_id}'"
                    )
            if not agent.workspace:
                errors.append(f"Agent '{agent.id}' has no workspace")
        return errors
