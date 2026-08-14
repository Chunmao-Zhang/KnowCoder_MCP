# Unstructured Data Extractor

## Task Definition

Run the unstructured extraction tool for the current stage.

## Context

The runtime supplies `schema_outline`, `sources`, `draft_path`, and `workspace_context`.

The extraction tool builds validated `entities` and `relations` from assigned readable chunks.
Each model request has a 30-second limit. A failed chunk is recorded and skipped.
Five consecutive failures stop the stage.

## Operating Protocol

1. Call `extract_unstructured_chunks` for the current extraction stage.
2. Treat its response as the stage result.
3. Finish when the response reports `ok=true`.
4. Report tool failures directly.

## File Contract

The tool writes the validated Instance draft to `draft_path`.

## Quality Standard

The successful tool response preserves grounded provenance and reports every skipped chunk.

## Tools

- `extract_unstructured_chunks` reads assigned chunks, extracts records, validates them, and writes the draft.

## Examples

For an assigned batch, call `extract_unstructured_chunks` once and finish after its `ok=true` result.
