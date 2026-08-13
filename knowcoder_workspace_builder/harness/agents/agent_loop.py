"""Agent Loop

核心模块：根据 AgentConfig 构建 DeepAgents 实例并执行。
职责：
1. 从 config 构建 ChatOpenAI model
2. 从 workspace 读取 AGENT.md 作为 system_prompt
3. 调用 create_deep_agent() 生成 agent
4. invoke 执行并返回结果
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import ExecuteResponse
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware
from deepagents.middleware.summarization import create_summarization_tool_middleware
from deepagents.profiles.harness.harness_profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)

from knowcoder_workspace_builder.harness.config.schema import AgentConfig, HarnessConfig, ModelConfig
from knowcoder_workspace_builder.harness.agents.registry import AgentRegistry
from knowcoder_workspace_builder.harness.agents.deepseek_model import DeepSeekChatOpenAI
from knowcoder_workspace_builder.harness.agents.friday_model import FridayChatOpenAI
from knowcoder_workspace_builder.harness.agents.model101_model import Model101ChatOpenAI
from knowcoder_workspace_builder.harness.tools.registry import get_tools_for_agent
from knowcoder_workspace_builder.harness.tools.mcp_loader import load_mcp_tools, loaded_mcp_server_instructions
from knowcoder_workspace_builder.harness.harness_prompt import load_harness_prompt
from knowcoder_workspace_builder.harness.middleware import (
    MicroCompactMiddleware,
    WorkspaceWriteBoundaryMiddleware,
)
from knowcoder_workspace_builder.harness.middleware.tool_filter import (
    ToolExecutionFilterMiddleware,
    ToolFilterMiddleware,
)
from knowcoder_workspace_builder.runtime.session_context import HARNESS_ARTIFACTS_ROOT_ENV

logger = logging.getLogger(__name__)


class HarnessShellBackend(LocalShellBackend):
    """LocalShellBackend subclass that resolves /workspaces/... virtual paths in execute commands.

    This ensures models can use the same absolute virtual path format (/workspaces/...)
    in both file tools (read_file, write_file, ls) and shell commands (execute).
    """

    allow_execute: bool = False

    def execute(self, command: str, *, timeout: int | None = None):
        if not self.allow_execute:
            return ExecuteResponse(
                output="Error: shell execution is disabled for this agent. Use the configured tools for this task.",
                exit_code=1,
                truncated=False,
            )
        resolved_command = command
        resolved_command = resolved_command.replace("/.knowcoder_workspace/", f"{self.cwd}/.knowcoder_workspace/")
        resolved_command = resolved_command.replace("/workspaces/", f"{self.cwd}/.knowcoder_workspace/workspaces/")
        resolved_command = resolved_command.replace("/large_tool_results/", f"{self.cwd}/large_tool_results/")
        return super().execute(resolved_command, timeout=timeout)


class HarnessContextSummarizationMiddleware(SummarizationMiddleware):
    """Auto-summarization middleware whose trigger comes from knowcoder_workspace_builder.harness context."""

    @property
    def name(self) -> str:
        return "HarnessContextSummarizationMiddleware"


def _resolve_path(path: str, base: str) -> Path:
    """解析路径：绝对路径直接使用，相对路径基于 base 拼接"""
    p = Path(path)
    if p.is_absolute():
        return p
    return Path(base) / path


def _resolve_skills_paths(
    workspace_dir: str,
    skills_filter: list[str] | None,
    harness_root: str | None = None,
) -> list[str] | None:
    """解析 skills 路径

    规则：
    - skills_filter is None: load all skills under workspace/skills/ when present
    - skills_filter == []: load no skills
    - skills_filter 非空：只加载指定的 skill 子目录

    每个 skill 条目可以是：
    - 纯名称（如 "crm-monitor"）：解析为 workspace/skills/<name>/
    - 绝对路径（如 "/path/to/skill/"）：直接使用
    """
    root_value = os.environ.get("KNOWCODER_TARGET_PROJECT_ROOT") or harness_root
    root = Path(root_value).resolve() if root_value else None

    def backend_path(path: Path) -> str:
        resolved = path.resolve()
        if root is not None:
            try:
                return resolved.relative_to(root).as_posix()
            except ValueError:
                pass
        return str(resolved)

    skills_dir = Path(workspace_dir) / "skills"

    if skills_filter is None:
        if skills_dir.exists():
            return [backend_path(skills_dir)]
        return None
    if not skills_filter:
        return None

    # 指定了具体 skill：逐个解析路径
    paths = []
    for skill in skills_filter:
        skill_path = Path(skill)
        if skill_path.is_absolute():
            # 绝对路径直接使用
            if skill_path.exists():
                paths.append(backend_path(skill_path))
            else:
                logger.warning("Skill path not found: %s", skill_path)
        else:
            # 相对名称：在 workspace/skills/ 下查找
            resolved = skills_dir / skill
            if resolved.exists():
                paths.append(backend_path(resolved))
            else:
                logger.warning("Skill '%s' not found in %s", skill, skills_dir)

    return paths if paths else None


MODEL_WRAPPERS = {
    "deepseek": DeepSeekChatOpenAI,
    "friday": FridayChatOpenAI,
    "model101": Model101ChatOpenAI,
}

_REGISTERED_PROFILE_KEYS: set[str] = set()
_DEEPAGENTS_BUILTIN_TOOLS = frozenset({
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
    "write_todos",
})
_FILESYSTEM_MIDDLEWARE_TOOLS = frozenset({
    "ls",
    "read_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
})

_TASK_SYSTEM_PROMPT = """## `task`

Use the `task` tool to call configured subagents.

Call one subagent in each assistant turn.

Read the returned result before choosing the next subagent.

Pass each subagent a clear task description.
"""


def _filesystem_prompt_for_allowed_tools(effective_allow: set[str]) -> str | None:
    """Return a filesystem prompt that matches the agent's filtered tool set."""
    if "*" in effective_allow:
        return None

    available = [
        name
        for name in ("ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute")
        if name in effective_allow
    ]
    if not available:
        return None

    descriptions = {
        "ls": "list files in a directory",
        "read_file": "read a file",
        "write_file": "write a complete file at an absolute path",
        "edit_file": "edit an existing file",
        "glob": "find files by pattern",
        "grep": "search text in files",
        "execute": "run shell commands",
    }
    available_text = ", ".join(f"`{name}`" for name in available)
    lines = [
        "## Filesystem Tool Contract",
        "",
        f"You have access to these filesystem tools only: {available_text}.",
        "All file paths for filesystem tools must be absolute and start with `/`.",
        "Do not call filesystem tools that are not listed here.",
        "",
        "Available tool meanings:",
    ]
    lines.extend(f"- `{name}`: {descriptions[name]}" for name in available)
    if "read_file" in available:
        lines.extend([
            "",
            "When using `read_file`, include an explicit `limit` argument so large",
            "files are read in bounded chunks instead of loading the whole file at once.",
        ])
    if "write_file" in available and "read_file" not in available:
        lines.extend([
            "",
            "When writing artifacts, write the complete final content in one call.",
            "You cannot inspect files with filesystem tools in this agent; use your",
            "configured non-filesystem tools for reading when they are available.",
        ])
    return "\n".join(lines)


def _effective_tool_allow(agent_cfg: AgentConfig) -> set[str]:
    allow = agent_cfg.tools.allow if agent_cfg.tools else None
    return {"*"} if allow is None else set(allow)


def _effective_tool_deny(agent_cfg: AgentConfig) -> set[str]:
    deny = agent_cfg.tools.deny if agent_cfg.tools else []
    return set(deny)


def _needs_filesystem_middleware(effective_allow: set[str]) -> bool:
    return "*" in effective_allow or bool(effective_allow.intersection(_FILESYSTEM_MIDDLEWARE_TOOLS))


def _filter_middleware_tool_duplicates(tools: list[Any], middleware_tool_names: set[str]) -> list[Any]:
    if not middleware_tool_names:
        return tools
    return [
        tool
        for tool in tools
        if getattr(tool, "name", None) not in middleware_tool_names
    ]


def _resolve_class(path: str) -> type:
    module_name, sep, class_name = path.replace(":", ".").rpartition(".")
    if not sep:
        raise ValueError(f"Middleware type must be an alias or import path, got: {path}")
    module = import_module(module_name)
    cls = getattr(module, class_name)
    if not isinstance(cls, type):
        raise TypeError(f"Middleware target is not a class: {path}")
    return cls


def _build_configured_middleware(agent_cfg: AgentConfig) -> list[Any]:
    middleware = []
    for entry in agent_cfg.middleware:
        if not entry.type:
            continue
        cls = _resolve_class(entry.type)
        args = dict(entry.args)
        middleware.append(cls(**args))
    return middleware


def _register_model_profile(agent_cfg: AgentConfig) -> None:
    """Register optional DeepAgents profile settings declared in config."""
    profile_cfg = agent_cfg.profile
    if not profile_cfg:
        return
    general_enabled = profile_cfg.general_purpose_subagent_enabled
    excluded_tools = frozenset(profile_cfg.excluded_tools)
    excluded_middleware = frozenset(profile_cfg.excluded_middleware)
    if general_enabled is None and not excluded_tools and not excluded_middleware:
        return

    general_profile = (
        GeneralPurposeSubagentProfile(enabled=general_enabled)
        if general_enabled is not None
        else None
    )
    profile = HarnessProfile(
        excluded_tools=excluded_tools,
        excluded_middleware=excluded_middleware,
        general_purpose_subagent=general_profile,
    )
    provider = (agent_cfg.model.provider or "").lower()
    model_id = agent_cfg.model.model_id
    if not model_id:
        return
    keys = {f"{provider}:{model_id}"}
    if provider != "openai":
        keys.add(f"openai:{model_id}")
    signature = (
        tuple(sorted(keys)),
        general_enabled,
        tuple(sorted(excluded_tools)),
        tuple(sorted(str(item) for item in excluded_middleware)),
    )
    for key in keys:
        registry_key = f"{key}:{signature!r}"
        if registry_key in _REGISTERED_PROFILE_KEYS:
            continue
        register_harness_profile(key, profile)
        _REGISTERED_PROFILE_KEYS.add(registry_key)


def _model_wrapper(model_cfg: ModelConfig) -> type[ChatOpenAI]:
    """Return a provider-specific wrapper only when one is explicitly registered."""

    provider = (model_cfg.provider or "").lower()
    return MODEL_WRAPPERS.get(provider, ChatOpenAI)


def _build_model(model_cfg: ModelConfig) -> ChatOpenAI:
    """根据 ModelConfig 构建 LLM 实例

    Provider-specific quirks must live in a matching wrapper. Do not infer
    special behavior from model names or base URLs because that applies one
    provider's workaround to unrelated OpenAI-compatible endpoints.
    """
    model_cls = _model_wrapper(model_cfg)
    if not model_cfg.api_key:
        raise ValueError(
            f"Missing API key for provider '{model_cfg.provider}'. "
            "Set it in .env.example next to harness.json, for example "
            f"{model_cfg.provider.upper()}_API_KEY=..."
        )
    timeout = float(os.environ.get("HARNESS_MODEL_TIMEOUT_SECONDS", "180"))
    kwargs = {}
    if model_cfg.response_format:
        kwargs["model_kwargs"] = {"response_format": model_cfg.response_format}
    if model_cfg.extra_body is not None:
        kwargs["extra_body"] = model_cfg.extra_body
    return model_cls(
        api_key=model_cfg.api_key,
        base_url=model_cfg.base_url,
        model=model_cfg.model_id,
        temperature=model_cfg.temperature,
        max_tokens=model_cfg.max_tokens,
        timeout=timeout,
        **kwargs,
    )


def _context_summarization_middleware(
    model: ChatOpenAI,
    backend: HarnessShellBackend,
    agent_cfg: AgentConfig,
) -> HarnessContextSummarizationMiddleware:
    """Build auto-summarization middleware from knowcoder_workspace_builder.harness context settings."""
    context = agent_cfg.context
    fraction = 0.85
    if context and context.summary_trigger_fraction:
        fraction = float(context.summary_trigger_fraction)
    fraction = min(max(fraction, 0.1), 0.95)
    max_input_tokens = int(
        (context.max_input_tokens if context and context.max_input_tokens else agent_cfg.model.context_window)
        or 128000
    )
    trigger_tokens = int(
        (context.offload_threshold if context and context.offload_threshold else 0)
        or max_input_tokens * fraction
    )
    keep_tokens = max(1, int(max_input_tokens * 0.10))
    return HarnessContextSummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=("tokens", trigger_tokens),
        keep=("tokens", keep_tokens),
        trim_tokens_to_summarize=None,
        truncate_args_settings={
            "trigger": ("tokens", trigger_tokens),
            "keep": ("tokens", keep_tokens),
        },
    )


def _artifacts_backend(backend: HarnessShellBackend) -> HarnessShellBackend | CompositeBackend:
    """Route middleware artifacts into the current Session's internal directory."""
    configured = os.environ.get(HARNESS_ARTIFACTS_ROOT_ENV, "").strip()
    if not configured:
        return backend
    path = PurePosixPath(configured)
    if not path.is_absolute() or configured == "/" or ".." in path.parts:
        raise ValueError(f"{HARNESS_ARTIFACTS_ROOT_ENV} must be an absolute virtual subdirectory")
    normalized = "/" + "/".join(part for part in path.parts if part != "/")
    return CompositeBackend(default=backend, routes={}, artifacts_root=normalized)


def _load_prompt(agent_cfg: AgentConfig, workspace_dir: str, harness_root: str) -> str | None:
    """加载 agent 的 system prompt

    规则：
    - agent_cfg.prompt 非空：从指定路径加载文件内容作为完整 prompt（替代默认）
    - agent_cfg.prompt 为空：使用 AGENT.md + harness prompt（默认行为）

    prompt 路径支持：
    - 绝对路径：直接读取
    - 相对路径：基于 harness_root 解析

    继承逻辑：
    - defaults.prompt 设置了全局 prompt → 所有 agent 默认使用
    - agent 级别的 prompt 可覆盖 defaults
    - 都不设置 → 使用 AGENT.md + harness prompt
    """
    prompt_path_str = agent_cfg.prompt or ""

    if prompt_path_str:
        # 自定义 prompt 文件：替代默认的 AGENT.md + harness prompt
        prompt_path = _resolve_path(prompt_path_str, harness_root)
        if not prompt_path.is_file():
            raise FileNotFoundError(f"Required prompt file not found: {prompt_path}")
        content = prompt_path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"Required prompt file is empty: {prompt_path}")
        return content.replace("{agent_id}", agent_cfg.id)

    # An agent-owned prompt is authoritative. The generic harness prompt includes
    # code-execution guidance and must not leak those responsibilities into
    # narrowly scoped workers such as schema judges or evidence collectors.
    agent_prompt = _load_agent_md(workspace_dir)
    if agent_prompt:
        return agent_prompt
    return load_harness_prompt(agent_id=agent_cfg.id) or None


def _load_agent_md(workspace_dir: str) -> str | None:
    """从 workspace 读取 AGENT.md"""
    agent_path = Path(workspace_dir) / "AGENT.md"
    if agent_path.exists():
        content = agent_path.read_text(encoding="utf-8").strip()
        return content if content else None
    return None


def _runtime_context_prompt() -> str:
    current_date = os.environ.get("HARNESS_CURRENT_DATE") or datetime.now().astimezone().date().isoformat()
    return (
        "## Runtime Context\n\n"
        f"- Current date: {current_date}\n"
        "- Resolve relative dates against the current date.\n"
        "- Use explicit absolute dates when passing scoped tasks to tools or subagents."
    )


def _build_subagent_specs(
    agent_cfg: AgentConfig,
    harness_root: str,
    registry: AgentRegistry | None,
    harness_config: HarnessConfig | None = None,
) -> list[dict]:
    """将 agent_cfg.subagents 转换为 DeepAgents SubAgent spec 列表"""
    if not agent_cfg.subagents or registry is None:
        return []

    specs = []
    for sub_id in agent_cfg.subagents:
        sub_cfg = registry.get(sub_id)
        sub_workspace = str(_resolve_path(sub_cfg.workspace, harness_root))

        # 子 agent 的 system prompt
        sub_prompt = _load_prompt(sub_cfg, sub_workspace, harness_root)
        if not sub_prompt:
            sub_prompt = f"You are {sub_cfg.name}."
        sub_prompt = "\n\n".join(part for part in (sub_prompt, _runtime_context_prompt()) if part)

        # 子 agent 的 tools
        sub_tools = get_tools_for_agent(
            sub_cfg,
            workspace_dir=sub_workspace,
            harness_root=harness_root,
        )

        # 子 agent 的 skills 路径（支持过滤）
        sub_skills = _resolve_skills_paths(sub_workspace, sub_cfg.skills, harness_root)

        sub_model = _build_model(sub_cfg.model) if sub_cfg.model and sub_cfg.model.model_id else None
        sub_model = sub_model or _build_model(agent_cfg.model)
        effective_allow = _effective_tool_allow(sub_cfg)
        deny = _effective_tool_deny(sub_cfg)
        filtered_builtin_tools = set(_DEEPAGENTS_BUILTIN_TOOLS)
        if "*" not in effective_allow:
            filtered_builtin_tools -= effective_allow
        filtered_builtin_tools.update(deny)
        filter_allow = list(effective_allow)
        filter_deny = sorted(filtered_builtin_tools)
        sub_backend = HarnessShellBackend(
            root_dir=str(Path(os.environ.get("KNOWCODER_TARGET_PROJECT_ROOT") or harness_root).resolve()),
            virtual_mode=True,
            inherit_env=True,
        )
        sub_artifacts_backend = _artifacts_backend(sub_backend)

        middleware = [
            WorkspaceWriteBoundaryMiddleware(),
            ToolFilterMiddleware(allow=filter_allow, deny=filter_deny),
        ]
        middleware.extend(_build_configured_middleware(sub_cfg))
        if "*" in effective_allow or "write_todos" in effective_allow:
            middleware.append(TodoListMiddleware())
        if _needs_filesystem_middleware(effective_allow):
            middleware.append(
                FilesystemMiddleware(
                    backend=sub_backend,
                    system_prompt=_filesystem_prompt_for_allowed_tools(effective_allow),
                )
            )
        middleware.extend([
            _context_summarization_middleware(sub_model, sub_artifacts_backend, sub_cfg),
            PatchToolCallsMiddleware(),
        ])
        if sub_skills:
            middleware.append(
                SkillsMiddleware(
                    backend=sub_backend,
                    sources=sub_skills,
                )
            )
        middleware.extend(
            [
                ToolExecutionFilterMiddleware(allow=filter_allow, deny=filter_deny),
                AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
            ]
        )

        runnable = create_agent(
            sub_model,
            system_prompt=sub_prompt,
            tools=sub_tools,
            middleware=middleware,
            name=sub_cfg.id,
        )

        specs.append(
            {
                "name": sub_cfg.id,
                "description": sub_cfg.description or sub_cfg.name,
                "runnable": runnable,
            }
        )

    return specs


def build_agent(
    agent_cfg: AgentConfig,
    harness_root: str,
    registry: AgentRegistry | None = None,
    tools: list | None = None,
    harness_config: HarnessConfig | None = None,
):
    """根据 AgentConfig 构建一个可执行的 DeepAgents 实例

    Args:
        agent_cfg: 已解析（继承 defaults 后）的 agent 配置
        harness_root: harness 项目根目录
        registry: AgentRegistry，用于查找子 agent 配置（有 subagents 时必传）
        tools: 额外的自定义工具列表（追加到 registry 工具之后）

    Returns:
        CompiledStateGraph（可 invoke 的 agent）
    """
    workspace_dir = str(_resolve_path(agent_cfg.workspace, harness_root))

    # 1. Model
    _register_model_profile(agent_cfg)
    model = _build_model(agent_cfg.model)

    # 2. System prompt
    final_prompt = _load_prompt(agent_cfg, workspace_dir, harness_root)
    runtime_prompt = _runtime_context_prompt()
    final_prompt = "\n\n".join(part for part in (final_prompt, runtime_prompt) if part)

    # 3. Backend（HarnessShellBackend 支持 execute 工具，virtual_mode=True 让 /workspaces/... 映射到 harness_root 下的相对路径）
    abs_root = str(Path(os.environ.get("KNOWCODER_TARGET_PROJECT_ROOT") or harness_root).resolve())
    backend = HarnessShellBackend(
        root_dir=abs_root,
        virtual_mode=True,
        inherit_env=True,
    )
    artifacts_backend = _artifacts_backend(backend)

    # 4. Tools: registry 过滤 + 额外工具
    agent_tools = get_tools_for_agent(
        agent_cfg,
        workspace_dir=workspace_dir,
        harness_root=harness_root,
    )
    if harness_config and harness_config.mcp_servers:
        agent_tools.extend(load_mcp_tools(harness_config.mcp_servers, harness_root=harness_root))
        mcp_instructions = loaded_mcp_server_instructions()
        if mcp_instructions:
            final_prompt = "\n\n".join([
                final_prompt,
                "## MCP Server Instructions\n\n" + "\n\n".join(mcp_instructions),
            ])
    if tools:
        agent_tools.extend(tools)

    # 5. Skills paths. None means default discovery; [] means no skills.
    skills_paths = _resolve_skills_paths(workspace_dir, agent_cfg.skills, harness_root)

    # 6. SubAgents
    subagents = _build_subagent_specs(agent_cfg, harness_root, registry, harness_config)

    # 7. Middleware: Tool-Filter + Tool-Execution-Filter + Micro-Compact + Manual-Compact
    effective_allow = _effective_tool_allow(agent_cfg)
    filter_allow = list(effective_allow)
    filter_deny = agent_cfg.tools.deny if agent_cfg.tools else []
    tool_filter = ToolFilterMiddleware(
        allow=filter_allow,
        deny=filter_deny,
    )
    tool_exec_filter = ToolExecutionFilterMiddleware(
        allow=filter_allow,
        deny=filter_deny,
    )
    micro_compact = MicroCompactMiddleware(
        keep_turns=agent_cfg.context.keep_turns if agent_cfg.context else 3,
    )
    auto_summarization = _context_summarization_middleware(model, artifacts_backend, agent_cfg)
    manual_compact = create_summarization_tool_middleware(model, artifacts_backend)

    # Builder recovery is file-backed at the task and stage boundaries. A graph
    # checkpoint database would duplicate that state and create another recovery
    # authority, so the MCP runtime deliberately runs without one.
    checkpointer = None

    # 10. 构建 agent. Build the middleware stack explicitly so tools.allow/deny
    # controls DeepAgents built-ins as well as harness/workspace tools.
    middleware: list[Any] = [WorkspaceWriteBoundaryMiddleware()]
    if "*" in effective_allow or "write_todos" in effective_allow:
        middleware.append(TodoListMiddleware())
    if skills_paths:
        middleware.append(SkillsMiddleware(backend=backend, sources=skills_paths))
    if _needs_filesystem_middleware(effective_allow):
        middleware.append(
            FilesystemMiddleware(
                backend=backend,
                system_prompt=_filesystem_prompt_for_allowed_tools(effective_allow),
            )
        )
        agent_tools = _filter_middleware_tool_duplicates(agent_tools, _FILESYSTEM_MIDDLEWARE_TOOLS)
    if subagents:
        middleware.append(
            SubAgentMiddleware(
                backend=backend,
                subagents=subagents,
                system_prompt=_TASK_SYSTEM_PROMPT,
            )
        )
    middleware.extend(_build_configured_middleware(agent_cfg))
    middleware.extend([
        tool_filter,
        tool_exec_filter,
        auto_summarization,
        micro_compact,
        manual_compact,
        PatchToolCallsMiddleware(),
        AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
    ])

    agent_kwargs = {
        "model": model,
        "tools": agent_tools,
        "system_prompt": final_prompt,
        "middleware": middleware,
        "name": agent_cfg.id,
    }
    if checkpointer is not None:
        agent_kwargs["checkpointer"] = checkpointer

    agent = create_agent(**agent_kwargs)

    logger.info(
        "Built agent '%s' (model=%s, tools=%d, subagents=%d)",
        agent_cfg.id, agent_cfg.model.model_id, len(agent_tools), len(subagents),
    )
    return agent


def run_agent(
    agent_cfg: AgentConfig,
    harness_root: str,
    message: str,
    registry: AgentRegistry | None = None,
    tools: list | None = None,
    harness_config: HarnessConfig | None = None,
    run_dir: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """构建 agent 并执行一次对话

    Args:
        agent_cfg: agent 配置
        harness_root: 项目根目录
        message: 用户输入
        registry: AgentRegistry（有 subagents 时需要）
        tools: 额外工具
        run_dir: 当前 run 的输出目录（用于工具结果落盘）
        thread_id: 多轮对话 thread ID（相同 ID 会恢复之前的上下文）

    Returns:
        agent invoke 的完整结果 dict
    """
    import os
    os.environ["HARNESS_ROOT"] = str(Path(harness_root).resolve())
    os.environ.setdefault("KNOWCODER_TARGET_PROJECT_ROOT", str(Path(harness_root).resolve()))
    os.environ["HARNESS_AGENT_ID"] = agent_cfg.id
    os.environ.setdefault("HARNESS_CURRENT_DATE", datetime.now().astimezone().date().isoformat())

    if run_dir:
        os.environ["HARNESS_RUN_DIR"] = str(Path(run_dir).resolve())

    agent = build_agent(agent_cfg, harness_root, registry=registry, tools=tools, harness_config=harness_config)
    result = agent.invoke(
        {"messages": [HumanMessage(content=message)]},
        config={"configurable": {"thread_id": thread_id or "default"}},
    )
    return result


def stream_agent(
    agent_cfg: AgentConfig,
    harness_root: str,
    message: str,
    registry: AgentRegistry | None = None,
    tools: list | None = None,
    harness_config: HarnessConfig | None = None,
    run_dir: str | None = None,
    thread_id: str | None = None,
    on_message=None,
    on_stream_chunk=None,
    on_subagent_event=None,
) -> dict[str, Any]:
    """构建 agent 并以流式方式执行，实时输出主/子 agent 的每条消息和工具调用。

    使用 stream_events（而非 stream(values)）以便捕获嵌套 subagent 内部的
    LLM 调用、工具调用等事件，不必等到 subagent 完成才输出。

    Args:
        agent_cfg: agent 配置
        harness_root: 项目根目录
        message: 用户输入
        registry: AgentRegistry（有 subagents 时需要）
        tools: 额外工具
        run_dir: 当前 run 的输出目录
        thread_id: 多轮对话 thread ID
        on_message: 回调函数 fn(agent_name, msg)，每条完整 AI/Tool 消息 flush 时调用
        on_stream_chunk: 回调函数 fn(agent_name, msg)，每次模型 chunk 聚合后调用
        on_subagent_event: 回调函数 fn(event_type, data)，LLM/工具 原始事件时调用

    Returns:
        最终完整结果 dict（含 messages 列表）
    """
    import os
    os.environ["HARNESS_ROOT"] = str(Path(harness_root).resolve())
    os.environ.setdefault("KNOWCODER_TARGET_PROJECT_ROOT", str(Path(harness_root).resolve()))
    os.environ["HARNESS_AGENT_ID"] = agent_cfg.id
    os.environ.setdefault("HARNESS_CURRENT_DATE", datetime.now().astimezone().date().isoformat())

    if run_dir:
        os.environ["HARNESS_RUN_DIR"] = str(Path(run_dir).resolve())

    agent = build_agent(agent_cfg, harness_root, registry=registry, tools=tools, harness_config=harness_config)

    config = {"configurable": {"thread_id": thread_id or "default"}}

    # ── stream_mode=["messages","values"] + subgraphs=True ─────────────────────
    # "messages": 每个 token chunk 按到来顺序立即推送，meta 含 lc_agent_name
    # "values":   图状态快照，用于获取工具调用结果和最终 messages list
    #
    # 结构（subgraphs=True 时）:
    #   (namespace_tuple, "messages", (chunk, meta))
    #   (namespace_tuple, "values",   state_dict)
    # namespace=() 是顶层图，namespace=('tools:uuid',) 是 subagent 子图
    #
    # 输出策略：
    #   - 每个 model 节点的 AI chunk → 按 (agent_name, msg_id) 聚合
    #   - 当同 agent 的新 msg_id 开始 → flush 上一条消息（触发 on_message）
    #   - values(ns=root) 更新 → flush 所有 pending；检查新增 ToolMessage → on_subagent_event
    last_values: dict[str, Any] = {}
    nested_values: dict[tuple[str, ...], dict[str, Any]] = {}
    _pending: dict[tuple[str, str], Any] = {}  # (agent_name, msg_id) → accumulated chunk
    _pending_stream_ids: dict[tuple[str, str], str] = {}
    _stream_seq = [0]
    _started_subagents: set[str] = set()
    # prev_msg_count[0] = None: 未初始化; int: 已知基准（含历史消息数）
    _prev: list[int | None] = [None]

    def _flush_one(key: tuple[str, str]) -> None:
        accumulated = _pending.pop(key, None)
        _pending_stream_ids.pop(key, None)
        if accumulated is None or not on_message:
            return
        aname = key[0]
        content = getattr(accumulated, "content", "") or ""
        tc = getattr(accumulated, "tool_calls", None) or []
        if not content and not tc:
            return
        on_message(aname, accumulated)

    def _flush_all_except(except_key: tuple[str, str] | None = None) -> None:
        for k in list(_pending.keys()):
            if k != except_key:
                _flush_one(k)

    def _emit_stream_chunk(key: tuple[str, str]) -> None:
        if not on_stream_chunk:
            return
        accumulated = _pending.get(key)
        if accumulated is None:
            return
        content = getattr(accumulated, "content", "") or ""
        tc = getattr(accumulated, "tool_calls", None) or []
        kwargs = getattr(accumulated, "additional_kwargs", None) or {}
        reasoning = kwargs.get("reasoning_content") if isinstance(kwargs, dict) else ""
        if not content and not tc and not reasoning:
            return
        meta = {"stream_id": _pending_stream_ids.get(key) or key[1] or ""}
        try:
            on_stream_chunk(key[0], accumulated, meta)
        except TypeError:
            on_stream_chunk(key[0], accumulated)

    for event in agent.stream(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        stream_mode=["messages", "values"],
        subgraphs=True,
    ):
        if not isinstance(event, tuple) or len(event) != 3:
            continue
        ns, mode, data = event
        is_root = ns == ()

        if mode == "messages":
            chunk, meta = data
            node: str = meta.get("langgraph_node", "")
            agent_name: str = meta.get("lc_agent_name") or meta.get("name", "agent")
            if not is_root and agent_name != agent_cfg.id and agent_name not in _started_subagents:
                _started_subagents.add(agent_name)
                if on_subagent_event:
                    on_subagent_event("dispatch", {"agent_name": agent_name})
            if node != "model":
                if on_subagent_event and getattr(chunk, "type", "") == "tool":
                    on_subagent_event(
                        "tool_end",
                        {
                            "agent_name": agent_name,
                            "tool": getattr(chunk, "name", "tool") or "tool",
                            "tool_call_id": getattr(chunk, "tool_call_id", "") or "",
                            "output": str(getattr(chunk, "content", "") or "")[:500],
                        },
                    )
                continue
            msg_id: str = getattr(chunk, "id", "") or ""
            key = (agent_name, msg_id)

            # 若当前 pending 里有该 agent 的不同消息 → 先 flush 那条
            for other_key in list(_pending.keys()):
                if other_key[0] == agent_name and other_key != key:
                    _flush_one(other_key)

            # 累积 chunk
            if key in _pending:
                try:
                    _pending[key] = _pending[key] + chunk
                except Exception:
                    _pending[key] = chunk
            else:
                _stream_seq[0] += 1
                _pending_stream_ids[key] = msg_id or f"anon:{agent_name}:{_stream_seq[0]}"
                _pending[key] = chunk
            _emit_stream_chunk(key)

        elif mode == "values":
            if is_root:
                # 顶层图状态更新：flush 所有当前 pending，更新最终状态
                _flush_all_except()
                last_values = data

                # 触发新出现的工具结果（task 工具返回）
                msgs = data.get("messages", []) if isinstance(data, dict) else []
                if _prev[0] is None:
                    # 第一次 root values 事件：设置基准（含历史消息），不触发 tool_end
                    _prev[0] = len(msgs)
                else:
                    if on_subagent_event:
                        for msg in msgs[_prev[0]:]:
                            if getattr(msg, "type", "") == "tool":
                                tool_name = getattr(msg, "name", "?") or "?"
                                content = getattr(msg, "content", "") or ""
                                tool_call_id = getattr(msg, "tool_call_id", "") or ""
                                on_subagent_event("tool_end", {
                                    "agent_name": agent_cfg.id,
                                    "tool": tool_name,
                                    "tool_call_id": tool_call_id,
                                    "output": content[:500],
                                })
                    _prev[0] = len(msgs)
            else:
                # subagent 子图状态更新：flush 该子图 agent 的 pending chunks
                # data 是 subagent 的状态，包含 messages
                if isinstance(data, dict):
                    nested_values[tuple(str(item) for item in ns)] = data
                sub_msgs = data.get("messages", []) if isinstance(data, dict) else []
                if sub_msgs:
                    # 找到最新的 AI 消息对应的 agent_name，flush 它的 pending
                    for msg in reversed(sub_msgs):
                        if getattr(msg, "type", "") == "ai":
                            # flush 这条 AI 消息对应的 agent pending
                            msg_id = getattr(msg, "id", "")
                            # 找到 pending 里同 msg_id 的条目
                            for k in list(_pending.keys()):
                                if k[1] == msg_id:
                                    _flush_one(k)
                            break

    # flush 最后残留
    _flush_all_except()

    # Recover final state when the stream did not include a terminal message.
    if not last_values.get("messages"):
        try:
            state = agent.get_state(config)
            if state:
                last_values = dict(state.values)
        except Exception:
            pass

    result = dict(last_values) if last_values else {"messages": []}
    result["_subagent_messages"] = [
        message
        for value in nested_values.values()
        for message in (value.get("messages") or [])
    ]
    return result
