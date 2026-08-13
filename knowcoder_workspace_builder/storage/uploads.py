"""Copy host uploads into one Session before any model can read them."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from knowcoder_workspace_builder.contracts.errors import ContractError, MissingStateError, StateConflictError, StorageBoundaryError
from knowcoder_workspace_builder.runtime.virtual_paths import VIRTUAL_ROOT, virtual_session_path

from .paths import SessionPaths, is_within
from .sources import SourceRepository


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned or "upload"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_name(path: Path) -> str:
    name = path.name
    name = re.sub(
        r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}-)+",
        "",
        name,
    )
    name = re.sub(r"^[0-9a-fA-F]{8,12}-", "", name)
    return name.replace("_", " ")


def _existing_upload_path(paths: SessionPaths, record: dict[str, object]) -> Path:
    value = str(record.get("file_path") or "").strip()
    prefix = VIRTUAL_ROOT + "/"
    if not value.startswith(prefix):
        raise StorageBoundaryError("Registered upload path must use the Session virtual root", path=value)
    target = paths.assert_writable(paths.root / value.removeprefix(prefix))
    if not target.is_file():
        raise MissingStateError("Registered upload file is missing", source_id=record.get("source_id"), path=value)
    return target


def ingest_uploads(paths: SessionPaths, values: list[str]) -> list[str]:
    repository = SourceRepository(paths)
    virtual_paths: list[str] = []
    destination_dir = paths.sources / "user_uploads"
    destination_dir.mkdir(parents=True, exist_ok=True)
    for raw in values:
        supplied = Path(str(raw or "").strip()).expanduser()
        if not supplied.is_absolute():
            raise StorageBoundaryError("Upload path must be absolute", path=str(raw))
        source = supplied.resolve(strict=True)
        if not source.is_file():
            raise ContractError("Upload path is not a file", path=str(source))
        if is_within(source, paths.data_root) and not is_within(source, paths.root):
            raise StorageBoundaryError("Upload belongs to a different Session", path=str(source))

        digest = _digest(source)
        source_id = f"upload-{digest[:20]}"
        existing = next(
            (item for item in repository.list() if str(item.get("source_id") or "") == source_id),
            None,
        )
        if existing is not None:
            existing_path = _existing_upload_path(paths, existing)
            if str(existing.get("source_kind") or "") != "upload" or _digest(existing_path) != digest:
                raise StateConflictError("Registered upload source does not match its content ID", source_id=source_id)
            virtual_paths.append(str(existing["file_path"]))
            continue
        display_name = _display_name(source)
        if is_within(source, destination_dir.resolve(strict=True)):
            destination = source
        else:
            destination = destination_dir / f"{digest[:12]}-{_safe_name(display_name)}"
            if destination.exists() and _digest(destination) != digest:
                raise StateConflictError("Upload destination contains different content", path=str(destination))
            if not destination.exists():
                shutil.copyfile(source, destination)
        virtual = virtual_session_path(destination.relative_to(paths.root).as_posix())
        repository.register(
            "user_uploads",
            {
                "source_id": source_id,
                "source_kind": "upload",
                "file_path": virtual,
                "file_type": destination.suffix.casefold().lstrip("."),
                "title": display_name,
                "size_bytes": destination.stat().st_size,
                "sha256": digest,
            },
        )
        virtual_paths.append(virtual)
    return virtual_paths
