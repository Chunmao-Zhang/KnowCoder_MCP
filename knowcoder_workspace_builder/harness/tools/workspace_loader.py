"""Load tool plugins from an agent workspace.

Workspace projects can define their own tool suite at:

    <project_workspace>/tools/__init__.py

Nested agent workspaces can also inherit the nearest ancestor tools directory,
for example:

    <project_workspace>/subagents/<agent>/AGENT.md
    <project_workspace>/tools/__init__.py

The module should expose `WORKSPACE_TOOLS = [...]`. It may also expose
`WORKSPACE_TOOLS_MODE = "replace"` to use only base harness tools plus the
workspace tools, instead of extending the central harness tool catalog.

If `tools/__init__.py` is absent, the loader auto-discovers top-level
`tools/*.py` files and instantiates BaseTool subclasses defined in those files.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from langchain_core.tools import BaseTool


@dataclass(frozen=True)
class WorkspaceToolsSpec:
    tools: list[BaseTool]
    mode: str = "extend"


def _as_tool_list(value: Any) -> list[BaseTool]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError("WORKSPACE_TOOLS must be a list or tuple of BaseTool instances.")

    tools: list[BaseTool] = []
    for item in value:
        if not isinstance(item, BaseTool):
            name = getattr(item, "name", repr(item))
            raise TypeError(f"Workspace tool {name!r} is not a LangChain BaseTool instance.")
        tools.append(item)
    return tools


def _module_name_for(tools_init: Path) -> str:
    digest = hashlib.sha1(str(tools_init.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"_harness_workspace_tools_{digest}"


def _load_module(tools_init: Path, harness_root: Path | None = None) -> ModuleType:
    if harness_root is not None:
        root_str = str(harness_root.resolve())
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

    module_name = _module_name_for(tools_init)
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(
        module_name,
        tools_init,
        submodule_search_locations=[str(tools_init.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load workspace tools module: {tools_init}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_package(package_name: str, package_dir: Path, harness_root: Path | None = None) -> None:
    if harness_root is not None:
        root_str = str(harness_root.resolve())
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

    existing = sys.modules.get(package_name)
    if existing is not None:
        return

    package = ModuleType(package_name)
    package.__file__ = str(package_dir)
    package.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
    package.__package__ = package_name
    sys.modules[package_name] = package


def _load_file_module(
    module_path: Path,
    package_name: str,
    package_dir: Path,
    harness_root: Path | None = None,
) -> ModuleType:
    _ensure_package(package_name, package_dir, harness_root)
    module_name = f"{package_name}.{module_path.stem}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load workspace tool file: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _tools_from_module(module: ModuleType) -> list[BaseTool]:
    if hasattr(module, "get_tools"):
        return _as_tool_list(module.get_tools())  # type: ignore[attr-defined]
    if hasattr(module, "WORKSPACE_TOOLS"):
        return _as_tool_list(getattr(module, "WORKSPACE_TOOLS"))
    if hasattr(module, "TOOLS"):
        return _as_tool_list(getattr(module, "TOOLS"))
    if hasattr(module, "TOOL"):
        return _as_tool_list([getattr(module, "TOOL")])

    discovered: list[BaseTool] = []
    seen_names: set[str] = set()

    for value in module.__dict__.values():
        if isinstance(value, BaseTool):
            if value.name not in seen_names:
                discovered.append(value)
                seen_names.add(value.name)

    for _, value in sorted(module.__dict__.items()):
        if not inspect.isclass(value):
            continue
        if not issubclass(value, BaseTool) or value is BaseTool:
            continue
        if value.__module__ != module.__name__:
            continue
        if value.__name__.startswith("_"):
            continue
        tool = value()
        if tool.name not in seen_names:
            discovered.append(tool)
            seen_names.add(tool.name)

    return discovered


def _discover_tool_files(
    tools_dir: Path,
    harness_root: Path | None = None,
) -> WorkspaceToolsSpec:
    workspace_dir = tools_dir.parent
    workspace_package = _module_name_for(workspace_dir / "__workspace__.py")
    tools_package = f"{workspace_package}.tools"
    _ensure_package(workspace_package, workspace_dir, harness_root)
    _ensure_package(tools_package, tools_dir, harness_root)
    tools: list[BaseTool] = []
    seen_names: set[str] = set()
    mode = "replace"

    for module_path in sorted(tools_dir.glob("*.py")):
        if module_path.name == "__init__.py" or module_path.stem.startswith("_"):
            continue
        module = _load_file_module(module_path, tools_package, tools_dir, harness_root)
        module_mode = getattr(module, "WORKSPACE_TOOLS_MODE", None)
        if module_mode:
            mode = str(module_mode).strip().lower() or mode
        for tool in _tools_from_module(module):
            if tool.name in seen_names:
                continue
            tools.append(tool)
            seen_names.add(tool.name)

    if mode not in {"extend", "replace"}:
        raise ValueError("WORKSPACE_TOOLS_MODE must be either 'extend' or 'replace'.")
    if not tools:
        mode = "extend"
    return WorkspaceToolsSpec(tools=tools, mode=mode)


def _find_tools_dir(workspace_path: Path, harness_root: Path | None = None) -> Path | None:
    """Find the closest tools directory for a workspace or nested agent workspace."""
    stop_at = harness_root.resolve() if harness_root is not None else None

    for current in [workspace_path, *workspace_path.parents]:
        tools_dir = current / "tools"
        if tools_dir.exists():
            return tools_dir
        if stop_at is not None and current == stop_at:
            break

    return None


def load_workspace_tools(
    workspace_dir: str | Path | None,
    harness_root: str | Path | None = None,
) -> WorkspaceToolsSpec:
    """Return tool plugins declared by the nearest workspace tools directory.

    Missing workspace-local or ancestor tools are normal and return an empty spec.
    """

    if not workspace_dir:
        return WorkspaceToolsSpec(tools=[])

    workspace_path = Path(workspace_dir)
    if not workspace_path.is_absolute() and harness_root is not None:
        workspace_path = Path(harness_root) / workspace_path
    workspace_path = workspace_path.resolve()

    root = Path(harness_root).resolve() if harness_root is not None else None
    tools_dir = _find_tools_dir(workspace_path, root)
    if tools_dir is None:
        return WorkspaceToolsSpec(tools=[])

    tools_init = tools_dir / "__init__.py"
    if not tools_init.exists():
        return _discover_tool_files(tools_dir, root)

    module = _load_module(tools_init, root)
    tools = _as_tool_list(getattr(module, "WORKSPACE_TOOLS", None))
    mode = str(getattr(module, "WORKSPACE_TOOLS_MODE", "extend")).strip().lower() or "extend"
    if mode not in {"extend", "replace"}:
        raise ValueError("WORKSPACE_TOOLS_MODE must be either 'extend' or 'replace'.")
    return WorkspaceToolsSpec(tools=tools, mode=mode)
