"""Load the protected Harness and expose declared stage ownership."""

from __future__ import annotations

from pathlib import Path

from knowcoder_workspace_builder.harness.agents.registry import AgentRegistry
from knowcoder_workspace_builder.harness.config.loader import load_config
from knowcoder_workspace_builder.runtime.model_override import apply_runtime_model_override
from knowcoder_workspace_builder.validation.stage_results import STAGE_PROTOCOLS


BUILDER_ROOT = Path(__file__).resolve().parents[1]
HARNESS_CONFIG = BUILDER_ROOT / "harness.json"


def load_harness_registry() -> tuple[object, AgentRegistry]:
    config = apply_runtime_model_override(load_config(HARNESS_CONFIG))
    registry = AgentRegistry(config)
    errors = registry.validate()
    if errors:
        raise ValueError("Invalid protected Harness configuration: " + "; ".join(errors))
    for stage, protocol in STAGE_PROTOCOLS.items():
        configured = registry.get(protocol.agent)
        if configured.id != protocol.agent:
            raise ValueError(f"Harness agent ownership mismatch for stage {stage}")
    coordinator = registry.get_default()
    if coordinator is None or coordinator.id != "workspace_builder":
        raise ValueError("Protected Harness must declare workspace_builder as its default Coordinator")
    return config, registry
