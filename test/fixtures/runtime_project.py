"""Create isolated selected projects inside the permitted runtime directory."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runtime_project() -> Path:
    case_root = SOURCE_ROOT / ".knowcoder_workspace" / "test_runs" / "pytest" / uuid4().hex
    project = case_root / "selected_project"
    project.mkdir(parents=True)
    try:
        yield project
    finally:
        shutil.rmtree(case_root, ignore_errors=False)
