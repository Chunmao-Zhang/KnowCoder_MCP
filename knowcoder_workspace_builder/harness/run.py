"""
统一运行入口

用法：
    # 基本用法
    python -m knowcoder_workspace_builder.harness.run --message "你的任务"

    # 指定 agent
    python -m knowcoder_workspace_builder.harness.run --agent main --message "你的任务"

    # 从文件读取任务
    python -m knowcoder_workspace_builder.harness.run --agent main --message-file task.txt

环境变量：
    SILICONFLOW_API_KEY - 模型 API 密钥
    SERPER_API_KEY      - 搜索 API 密钥（可选）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from knowcoder_workspace_builder.harness.config import load_config
from knowcoder_workspace_builder.harness.agents.registry import AgentRegistry
from knowcoder_workspace_builder.harness.agents.agent_loop import run_agent, stream_agent, _load_prompt, _resolve_path
from knowcoder_workspace_builder.harness.runtime import RuntimeContext, RunLifecycle
from knowcoder_workspace_builder.harness.export.sft_recorder import record_run


def main():
    parser = argparse.ArgumentParser(description="Agent Harness 运行入口")
    parser.add_argument("--agent", default=None, help="要运行的 agent ID (不指定则使用 default agent)")
    parser.add_argument("--message", "-m", help="任务指令")
    parser.add_argument("--message-file", "-f", help="从文件读取任务指令")
    parser.add_argument("--config", default="harness.json", help="配置文件路径")
    parser.add_argument("--thread-id", "-t", help="多轮对话 thread ID（不传则每次新建）")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印详细消息流")
    args = parser.parse_args()

    # 获取任务
    if args.message:
        message = args.message
    elif args.message_file:
        with open(args.message_file, "r", encoding="utf-8") as f:
            message = f.read().strip()
    else:
        parser.error("必须指定 --message 或 --message-file")

    harness_root = _PROJECT_ROOT
    os.chdir(harness_root)

    # 加载配置
    cfg = load_config(args.config)
    registry = AgentRegistry(cfg)

    # 确定要运行的 agent
    if args.agent:
        agent_id = args.agent
    else:
        default_agent = registry.get_default()
        if default_agent:
            agent_id = default_agent.id
        else:
            agent_id = registry.list_all()[0].id if registry.list_all() else "main"

    agent_cfg = registry.get(agent_id)

    # 创建 run
    ctx = RuntimeContext(agent_id=agent_id, harness_root=".", max_steps=agent_cfg.max_steps)
    lc = RunLifecycle(ctx)
    lc.start()

    print(f"[run_id: {ctx.run_id}] agent={agent_id} model={agent_cfg.model.model_id}")
    if args.thread_id:
        print(f"[thread: {args.thread_id}] (多轮对话模式)")
    print(f"[input] {message[:200]}")
    print()

    # 执行
    thread_id = args.thread_id or str(ctx.run_dir)

    # 主 agent 名，用于在输出中区分主 / 子 agent
    main_agent_id = agent_cfg.id

    def print_message(agent_name: str, msg):
        """实时打印 LLM 输出（主 agent 和 subagent 均走这里）"""
        # 判断是否是子 agent（名字不同于主 agent）
        is_sub = agent_name and agent_name != main_agent_id
        indent = "    " if is_sub else "  "

        tc = getattr(msg, "tool_calls", None)
        c = getattr(msg, "content", "") or ""

        if tc:
            for call in tc:
                args_str = json.dumps(call.get("args", {}), ensure_ascii=False)
                if len(args_str) > 300:
                    args_str = args_str[:300] + "..."
                print(f"{indent}[CALL] {call['name']}({args_str})")
        if c:
            label = "[ASSISTANT]" if not is_sub else "[REPLY]"
            print(f"{indent}{label} {c[:600]}")
        print()

    def print_subagent_event(event_type, data):
        """实时打印工具调用事件（tool_start / tool_end）"""
        agent = data.get("agent", "?")
        is_sub = agent and agent != main_agent_id
        indent = "    " if is_sub else "  "
        tag = f"[{agent}]" if is_sub else ""

        if event_type == "tool_start":
            tool = data.get("tool", "?")
            inp = data.get("input", "")
            print(f"{indent}{tag}[TOOL→] {tool}({inp[:200]})")
        elif event_type == "tool_end":
            tool = data.get("tool", "?")
            out = data.get("output", "")
            print(f"{indent}{tag}[←TOOL] {tool}: {str(out)[:300]}")
            print()

    if args.verbose:
        # 流式模式：stream_events 实时输出主/子 agent 每条 LLM + 工具事件
        result = stream_agent(
            agent_cfg, harness_root=".", message=message,
            registry=registry, harness_config=cfg, run_dir=str(ctx.run_dir),
            thread_id=thread_id, on_message=print_message,
            on_subagent_event=print_subagent_event,
        )
    else:
        # 非流式：只输出最终结果
        result = run_agent(
            agent_cfg, harness_root=".", message=message,
            registry=registry, harness_config=cfg, run_dir=str(ctx.run_dir),
            thread_id=thread_id,
        )

    messages = result.get("messages", [])

    # 非 verbose 模式打印最终回复
    if not args.verbose:
        for msg in reversed(messages):
            if getattr(msg, "type", "") == "ai" and getattr(msg, "content", ""):
                print(f"[output] {msg.content}")
                break

    # 记录 SFT（包含 system prompt）
    workspace_dir = str(_resolve_path(agent_cfg.workspace, harness_root))
    system_prompt = _load_prompt(agent_cfg, workspace_dir, harness_root)
    output_path = ctx.run_dir / "messages.jsonl"
    record_run(messages, output_path, metadata={"run_id": ctx.run_id, "agent_id": agent_id}, system_prompt=system_prompt)
    ctx.step()
    lc.finish()

    print()
    print(f"[saved] {output_path}")


if __name__ == "__main__":
    main()
