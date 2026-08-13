from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from knowcoder_workspace_builder.contracts.errors import StateConflictError, StorageBoundaryError
from knowcoder_workspace_builder.storage.events import EventStore
from knowcoder_workspace_builder.storage.locks import SessionLockStore
from knowcoder_workspace_builder.storage.readme import validate_workspace_readme
from knowcoder_workspace_builder.storage.paths import IGNORE_CONTENT, ProjectLayout
from knowcoder_workspace_builder.storage.sessions import BuildStateStore
from knowcoder_workspace_builder.storage.transaction import AtomicWriter
from knowcoder_workspace_builder.storage.workspace import WorkspaceRepository


SCHEMA_SOURCE = '''class Entity:
    name: str


class Subject(Entity):
    """A researched subject."""
    _id: str
    name: str
'''


def _workspace_readme(label: str) -> str:
    return f'''---
name: "{label}"
description: "A test Workspace publication."
finish:
  completed: true
  details: "The test Workspace publication is complete."
---

# Workspace Overview

{label}

## Completed Research

- Test the publication transaction.

## Schema and Data

- One entity type.

## Main Files

- `ontology/types.py`

## Sources

- No external sources.

## Incremental Extension

Add later evidence to this Workspace.
'''


def test_runtime_creates_only_schema_workspace_in_selected_project(runtime_project) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session(str(uuid4()), create=True)

    assert [path.name for path in runtime_project.iterdir()] == [".knowcoder_workspace"]
    assert sorted(path.name for path in paths.root.iterdir()) == ["intermediate", "workspace"]


def test_runtime_root_initialization_is_atomic_across_threads(runtime_project) -> None:
    layout = ProjectLayout(runtime_project)

    with ThreadPoolExecutor(max_workers=16) as pool:
        roots = list(pool.map(lambda _index: layout.ensure_data_root(), range(64)))

    assert set(roots) == {layout.data_root}
    assert (layout.data_root / ".gitignore").read_text(encoding="utf-8") == IGNORE_CONTENT


def test_runtime_accepts_equivalent_ignore_rules_with_a_legacy_comment(runtime_project) -> None:
    layout = ProjectLayout(runtime_project)
    layout.data_root.mkdir()
    (layout.data_root / ".gitignore").write_text(
        "# Legacy product name does not change ignore behavior.\n*\n!.gitignore\n",
        encoding="utf-8",
    )

    assert layout.ensure_data_root() == layout.data_root


def test_runtime_rejects_ignore_file_that_can_publish_workspace_data(runtime_project) -> None:
    layout = ProjectLayout(runtime_project)
    layout.data_root.mkdir()
    (layout.data_root / ".gitignore").write_text("!.gitignore\n", encoding="utf-8")

    with pytest.raises(StorageBoundaryError, match="must ignore Workspace data"):
        layout.ensure_data_root()


def test_session_lock_is_reentrant_within_one_thread(runtime_project) -> None:
    layout = ProjectLayout(runtime_project)
    session_id = str(uuid4())
    layout.session(session_id, create=True)
    locks = SessionLockStore(layout)

    with locks.acquire(session_id):
        with locks.acquire(session_id):
            assert True


def test_session_rejects_a_third_root_entry(runtime_project) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session(str(uuid4()), create=True)
    (paths.root / "tool_results").mkdir()

    with pytest.raises(StorageBoundaryError, match="only workspace and intermediate"):
        layout.session(paths.session_id)


def test_session_write_boundary_rejects_a_third_root_entry(runtime_project) -> None:
    paths = ProjectLayout(runtime_project).session(str(uuid4()), create=True)

    with pytest.raises(StorageBoundaryError, match="workspace or intermediate"):
        paths.assert_writable(paths.root / "reports" / "result.json")


def test_session_write_boundary_rejects_parent_escape(runtime_project) -> None:
    paths = ProjectLayout(runtime_project).session(str(uuid4()), create=True)

    with pytest.raises(StorageBoundaryError, match="outside the current Session"):
        paths.assert_writable(paths.root / ".." / "other-session" / "state.json")


def test_stale_state_update_cannot_overwrite_newer_state(runtime_project) -> None:
    store = BuildStateStore(ProjectLayout(runtime_project))
    state = store.create("Compare all supplied records.", [])

    updated = store.update(state.session_id, 1, lambda current: current)
    assert updated.version == 2
    with pytest.raises(StateConflictError, match="changed before"):
        store.update(state.session_id, 1, lambda current: current)


def test_concurrent_events_receive_one_stable_sequence(runtime_project) -> None:
    layout = ProjectLayout(runtime_project)
    state = BuildStateStore(layout).create("Build a research workspace.", [])
    store = EventStore(layout)

    def append(index: int) -> int:
        return store.append(
            state.session_id,
            kind="invocation",
            status="running",
            invocation_id=f"invocation-{index}",
            agent="problem_clarifier",
            stage="clarify",
        ).sequence

    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(append, range(24)))

    assert sorted(sequences) == list(range(1, 25))
    assert [item["sequence"] for item in store.read(state.session_id)] == list(range(1, 25))


def test_different_sessions_have_disjoint_event_logs(runtime_project) -> None:
    layout = ProjectLayout(runtime_project)
    states = [BuildStateStore(layout).create(f"Question {index}", []) for index in range(2)]
    store = EventStore(layout)
    for state in states:
        store.append(state.session_id, kind="stage_state", status="running", stage="clarify")

    first = store.read(states[0].session_id)
    second = store.read(states[1].session_id)
    assert first[0]["session_id"] != second[0]["session_id"]


def test_workspace_publication_rolls_back_when_current_pointer_write_fails(
    runtime_project,
    monkeypatch,
) -> None:
    paths = ProjectLayout(runtime_project).session("session-publication-rollback", create=True)
    repository = WorkspaceRepository(paths)
    repository.commit(
        schema_source=SCHEMA_SOURCE,
        instances={"entities": [], "relations": []},
        sources=[],
        schema_version=1,
        data_version=1,
        readme=_workspace_readme("First publication"),
    )
    pointer = paths.intermediate / "workspace_versions" / "current.json"
    original_pointer = json.loads(pointer.read_text(encoding="utf-8"))
    original_readme = (paths.workspace / "README.md").read_text(encoding="utf-8")
    original_json = AtomicWriter.json

    def fail_current_pointer(self, target, value):
        if target == pointer:
            raise OSError("simulated current pointer failure")
        return original_json(self, target, value)

    monkeypatch.setattr(AtomicWriter, "json", fail_current_pointer)

    with pytest.raises(OSError, match="simulated current pointer failure"):
        repository.commit(
            schema_source=SCHEMA_SOURCE,
            instances={"entities": [], "relations": []},
            sources=[],
            schema_version=1,
            data_version=2,
            readme=_workspace_readme("Second publication"),
        )

    assert json.loads(pointer.read_text(encoding="utf-8")) == original_pointer
    assert (paths.workspace / "README.md").read_text(encoding="utf-8") == original_readme
    snapshots = [item for item in pointer.parent.iterdir() if item.is_dir()]
    assert [item.name for item in snapshots] == [original_pointer["publication_id"]]


@pytest.mark.parametrize(
    "invalid_line, message",
    [
        (r"- `ontology\\types.py`", "forward slashes"),
        ("- `/.knowcoder_workspace/sessions/private/workspace/README.md`", "Workspace-relative"),
    ],
)
def test_workspace_readme_rejects_internal_or_platform_specific_paths(invalid_line, message) -> None:
    readme = _workspace_readme("Invalid path publication").replace("- `ontology/types.py`", invalid_line)
    with pytest.raises(Exception, match=message):
        validate_workspace_readme(readme)
