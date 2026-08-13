"""Schema outline compile path used by Schema Engineer acceleration."""

from __future__ import annotations

import pytest

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.storage.schema import compile_schema_payload, parse_schema, schema_from_review
from knowcoder_workspace_builder.validation.validators.completion_validators import SchemaBuildCompletionValidator


def test_compile_entities_relations_outline_to_python() -> None:
    outline = {
        "entities": [
            {
                "name": "Team",
                "description": "A national team.",
                "id_type": "str",
                "attributes": [{"name": "fifa_rank", "type": "int", "optional": True}],
            },
            {
                "name": "Observation",
                "description": "A sourced metric.",
                "id_type": "str",
                "attributes": [
                    {"name": "metric_name", "type": "str"},
                    {"name": "numeric_value", "type": "float", "optional": True},
                ],
            },
        ],
        "relations": [
            {
                "name": "observation_subject_team",
                "head": "Observation",
                "tail": "Team",
                "description": "Links observation to team.",
                "many": False,
                "optional": True,
            }
        ],
    }
    source = compile_schema_payload(outline)
    parsed = parse_schema(source, require_relations=False)
    assert "Team" in parsed.entity_names
    assert "observation_subject_team" in parsed.relation_names
    assert 'Optional["Team"]' in source


def test_schema_build_validator_accepts_runtime_compiled_artifact_payload() -> None:
    payload = {
        "entities": [
            {
                "name": "Company",
                "description": "A company.",
                "id_type": "str",
                "attributes": [{"name": "sector", "type": "str", "optional": True}],
            }
        ],
        "relations": [],
    }
    source = compile_schema_payload(payload)
    outcome = SchemaBuildCompletionValidator().validate({"schema_source": source, "schema_outline": {}})
    assert outcome.ok is True
    assert outcome.context["entity_count"] == 1


def test_model_schema_source_payload_is_rejected() -> None:
    source = """
class Entity:
    _id: str
    name: str

class Note(Entity):
    \"\"\"A freeform note.\"\"\"
    _id: str
    name: str
"""
    with pytest.raises(ContractError, match="entities and relations blueprint"):
        compile_schema_payload({"schema_source": source})


def test_schema_from_review_allows_zero_relations() -> None:
    source = schema_from_review(
        {
            "entities": [
                {
                    "name": "Note",
                    "description": "A note entity.",
                    "id_type": "str",
                    "attributes": [],
                }
            ],
            "relations": [],
        }
    )
    parsed = parse_schema(source, require_relations=False)
    assert parsed.entity_names == frozenset({"Note"})


def test_missing_entity_description_fails() -> None:
    with pytest.raises(ContractError):
        schema_from_review(
            {
                "entities": [{"name": "Team", "description": "", "id_type": "str", "attributes": []}],
                "relations": [],
            }
        )
