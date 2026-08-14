"""Model-token-aware text chunking shared by Schema and extraction stages."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import tiktoken
from tiktoken.load import load_tiktoken_bpe

TOKENIZER_MODEL_ENV = "UNSTRUCTURED_EXTRACTION_MODEL"
DEFAULT_TOKENIZER_MODEL = "gpt-4o-mini"
TOKENIZER_DATA = Path(__file__).with_name("tokenizer_data") / "o200k_base.tiktoken"
O200K_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
O200K_PATTERN = "|".join(
    [
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    ]
)


@lru_cache(maxsize=8)
def _encoding(model: str):
    if not TOKENIZER_DATA.is_file():
        raise RuntimeError(f"Tokenizer data is missing: {TOKENIZER_DATA}")
    if "gpt-4o" not in model.casefold() and "o200k" not in model.casefold():
        raise ValueError(f"No packaged tokenizer is configured for model {model!r}")
    return tiktoken.Encoding(
        name="knowcoder_o200k_base",
        pat_str=O200K_PATTERN,
        mergeable_ranks=load_tiktoken_bpe(str(TOKENIZER_DATA), expected_hash=O200K_SHA256),
        special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
    )


def tokenizer_model() -> str:
    model = str(os.environ.get(TOKENIZER_MODEL_ENV) or DEFAULT_TOKENIZER_MODEL).strip()
    if not model:
        raise ValueError(f"{TOKENIZER_MODEL_ENV} must contain a model name")
    return model


def token_count(text: str, *, model: str | None = None) -> int:
    return len(_encoding(model or tokenizer_model()).encode(str(text or "")))


def token_chunks(
    text: str,
    *,
    target_tokens: int,
    overlap_tokens: int,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Split text by model tokens while retaining character offsets for provenance."""
    content = str(text or "")
    if not content.strip():
        raise ValueError("Token chunk content is empty")
    if target_tokens <= 0:
        raise ValueError("Token chunk target must be positive")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("Token chunk overlap must be non-negative and smaller than the target")
    active_model = model or tokenizer_model()
    encoding = _encoding(active_model)
    tokens = encoding.encode(content)
    if not tokens:
        raise ValueError("Token chunk content produced no tokens")
    _decoded, offsets = encoding.decode_with_offsets(tokens)
    step = target_tokens - overlap_tokens
    chunks: list[dict[str, Any]] = []
    token_start = 0
    while token_start < len(tokens):
        token_end = min(len(tokens), token_start + target_tokens)
        start = offsets[token_start]
        end = offsets[token_end] if token_end < len(tokens) else len(content)
        while end <= start and token_end < len(tokens):
            token_end += 1
            end = offsets[token_end] if token_end < len(tokens) else len(content)
        raw_text = content[start:end]
        left_trim = len(raw_text) - len(raw_text.lstrip())
        right_trim = len(raw_text) - len(raw_text.rstrip())
        start += left_trim
        end -= right_trim
        chunk_text = content[start:end]
        while chunk_text and token_count(chunk_text, model=active_model) > target_tokens:
            end -= 1
            chunk_text = content[start:end].rstrip()
        if chunk_text:
            chunks.append(
                {
                    "start": start,
                    "end": end,
                    "text": chunk_text,
                    "token_count": token_count(chunk_text, model=active_model),
                    "tokenizer_model": active_model,
                }
            )
        if token_end >= len(tokens):
            break
        token_start += step
    return chunks
