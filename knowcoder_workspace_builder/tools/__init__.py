"""Schema workspace tools."""

from .evidence_retriever import web_search, web_search_batch
from .schema_candidate_builder import build_schema_candidates
from .schema_outline import get_schema_outline
from .schema_validator import schema_validator
from .source_reader import source_reader
from .stage_artifacts import (
    append_instances_batch,
    append_instances_batches_from_file,
    save_evidence_manifest,
    save_problem_review,
    save_schema,
    save_schema_judgement,
    save_workspace_readme,
)
from .unstructured_extractor import extract_unstructured_chunks
from .web_fetch import fetch_web_pages
from .workspace_readme_browser import workspace_readme_browser

WORKSPACE_TOOLS = [
    workspace_readme_browser,
    source_reader,
    build_schema_candidates,
    extract_unstructured_chunks,
    web_search,
    web_search_batch,
    fetch_web_pages,
    schema_validator,
    save_problem_review,
    save_evidence_manifest,
    save_schema,
    save_schema_judgement,
    save_workspace_readme,
    get_schema_outline,
    append_instances_batch,
    append_instances_batches_from_file,
]

WORKSPACE_TOOLS_MODE = "extend"
