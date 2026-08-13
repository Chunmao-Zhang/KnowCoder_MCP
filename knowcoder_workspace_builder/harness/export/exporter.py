"""SFT 批量导出器

从 runs/ 目录中收集所有 messages.jsonl，合并为一个训练数据集文件。

用法：
    python -m knowcoder_workspace_builder.harness.export.exporter --output sft_dataset.jsonl
    python -m knowcoder_workspace_builder.harness.export.exporter --output sft_dataset.jsonl --min-messages 4
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def collect_runs(runs_dir: str | Path) -> list[dict]:
    """收集 runs/ 下所有 messages.jsonl 中的样本

    Returns:
        样本列表，每个样本是 {"messages": [...], "metadata": {...}}
    """
    runs_dir = Path(runs_dir)
    samples = []

    if not runs_dir.exists():
        return samples

    for jsonl_path in sorted(runs_dir.rglob("messages.jsonl")):
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    sample = json.loads(line)
                    samples.append(sample)
                except json.JSONDecodeError:
                    continue

    return samples


def filter_samples(
    samples: list[dict],
    min_messages: int = 2,
    require_tool_calls: bool = False,
) -> list[dict]:
    """过滤样本

    Args:
        samples: 原始样本列表
        min_messages: 最少消息数（过滤太短的对话）
        require_tool_calls: 是否要求包含 tool_calls
    """
    filtered = []
    for sample in samples:
        msgs = sample.get("messages", [])
        if len(msgs) < min_messages:
            continue
        if require_tool_calls:
            has_tc = any(m.get("tool_calls") for m in msgs)
            if not has_tc:
                continue
        filtered.append(sample)
    return filtered


def export_dataset(
    runs_dir: str | Path,
    output_path: str | Path,
    min_messages: int = 2,
    require_tool_calls: bool = False,
) -> int:
    """从 runs/ 导出 SFT 数据集

    Args:
        runs_dir: runs 目录路径
        output_path: 输出文件路径
        min_messages: 最少消息数
        require_tool_calls: 是否要求包含工具调用

    Returns:
        导出的样本数
    """
    samples = collect_runs(runs_dir)
    filtered = filter_samples(samples, min_messages, require_tool_calls)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for sample in filtered:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    return len(filtered)


def main():
    parser = argparse.ArgumentParser(description="SFT 数据批量导出")
    parser.add_argument("--runs-dir", default="runs", help="runs 目录路径")
    parser.add_argument("--output", "-o", default="sft_dataset.jsonl", help="输出文件路径")
    parser.add_argument("--min-messages", type=int, default=2, help="最少消息数")
    parser.add_argument("--require-tool-calls", action="store_true", help="只保留包含工具调用的样本")
    args = parser.parse_args()

    count = export_dataset(
        runs_dir=args.runs_dir,
        output_path=args.output,
        min_messages=args.min_messages,
        require_tool_calls=args.require_tool_calls,
    )

    print(f"Exported {count} samples to {args.output}")


if __name__ == "__main__":
    main()
