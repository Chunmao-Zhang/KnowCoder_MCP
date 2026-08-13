"""Parse, validate, and compile the KO/OI-compatible schema file."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.contracts.workspace import SCHEMA_ID_TYPES, SCHEMA_PRIMITIVES



@dataclass(frozen=True)
class SchemaField:
    name: str
    kind: str
    value_type: str
    optional: bool = False
    many: bool = False
    description: str = ""


@dataclass(frozen=True)
class EntitySchema:
    name: str
    description: str
    id_type: str
    attributes: tuple[SchemaField, ...]
    relations: tuple[SchemaField, ...]


@dataclass(frozen=True)
class ParsedSchema:
    entities: tuple[EntitySchema, ...]

    @property
    def entity_names(self) -> frozenset[str]:
        return frozenset(item.name for item in self.entities)

    @property
    def relation_names(self) -> frozenset[str]:
        return frozenset(field.name for item in self.entities for field in item.relations)

    def outline(self) -> dict[str, Any]:
        return {
            "entities": [
                {
                    "entity_type": item.name,
                    "id_type": item.id_type,
                    "description": item.description,
                    "attributes": [
                        {
                            "name": field.name,
                            "type": field.value_type,
                            "optional": field.optional,
                            "many": field.many,
                        }
                        for field in item.attributes
                    ],
                    "relations": [
                        {
                            "name": field.name,
                            "target": field.value_type,
                            "optional": field.optional,
                            "many": field.many,
                            "description": field.description,
                        }
                        for field in item.relations
                    ],
                }
                for item in self.entities
            ],
            "entity_count": len(self.entities),
            "relation_count": sum(len(item.relations) for item in self.entities),
        }


def _node_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _subscript(node: ast.AST) -> tuple[str | None, ast.AST | None]:
    if not isinstance(node, ast.Subscript):
        return None, None
    return _node_name(node.value), node.slice


def _field_from_annotation(name: str, annotation: ast.AST, entity_names: set[str]) -> SchemaField:
    direct = _node_name(annotation)
    if direct in SCHEMA_PRIMITIVES:
        return SchemaField(name=name, kind="attribute", value_type=direct)
    if direct in entity_names:
        return SchemaField(name=name, kind="relation", value_type=direct)

    outer, inner = _subscript(annotation)
    if inner is None:
        raise ContractError("Schema field has an unsupported type", field=name, annotation=ast.unparse(annotation))
    target = _node_name(inner)
    if outer == "Optional":
        if target in SCHEMA_PRIMITIVES:
            return SchemaField(name=name, kind="attribute", value_type=target, optional=True)
        if target in entity_names:
            return SchemaField(name=name, kind="relation", value_type=target, optional=True)
    if outer in {"List", "list"} and target in entity_names:
        return SchemaField(name=name, kind="relation", value_type=target, many=True)
    raise ContractError("Schema field has an unsupported type", field=name, annotation=ast.unparse(annotation))


def _attribute_docstrings(node: ast.ClassDef) -> dict[str, str]:
    """Return PEP 258 attribute docstrings keyed by their annotated field name."""
    descriptions: dict[str, str] = {}
    for index, statement in enumerate(node.body[:-1]):
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        following = node.body[index + 1]
        if (
            isinstance(following, ast.Expr)
            and isinstance(following.value, ast.Constant)
            and isinstance(following.value.value, str)
        ):
            descriptions[statement.target.id] = following.value.value.strip()
    return descriptions


def _declaration_issues(entity_nodes: list[ast.ClassDef], entity_names: set[str]) -> list[dict[str, str]]:
    """Collect mechanical declaration gaps so one repair can fix the whole schema."""
    issues: list[dict[str, str]] = []
    for node in entity_nodes:
        annotations = [
            statement
            for statement in node.body
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
        ]
        by_name = {statement.target.id: statement for statement in annotations}
        id_node = by_name.get("_id")
        if id_node is None or _node_name(id_node.annotation) not in SCHEMA_ID_TYPES:
            issues.append({"kind": "entity_id", "entity": node.name, "required": "_id: str or _id: int"})
        name_node = by_name.get("name")
        if name_node is None or _node_name(name_node.annotation) != "str":
            issues.append({"kind": "entity_name", "entity": node.name, "required": "name: str"})
        if not (ast.get_docstring(node) or "").strip():
            issues.append({"kind": "entity_description", "entity": node.name, "required": "class docstring"})
        field_descriptions = _attribute_docstrings(node)
        for statement in annotations:
            field_name = statement.target.id
            if field_name in {"_id", "name"}:
                continue
            try:
                field = _field_from_annotation(field_name, statement.annotation, entity_names)
            except ContractError:
                continue
            if field.kind == "relation" and not field_descriptions.get(field_name, ""):
                issues.append(
                    {
                        "kind": "relation_description",
                        "entity": node.name,
                        "relation": field_name,
                        "required": "attribute docstring",
                    }
                )
    return issues


def _unsupported_field_issues(
    entity_nodes: list[ast.ClassDef],
    entity_names: set[str],
) -> list[dict[str, str]]:
    """Collect every unsupported annotation before parsing individual entities."""
    issues: list[dict[str, str]] = []
    for node in entity_nodes:
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                continue
            field_name = statement.target.id
            if field_name == "_id":
                continue
            try:
                _field_from_annotation(field_name, statement.annotation, entity_names)
            except ContractError:
                issues.append(
                    {
                        "entity": node.name,
                        "field": field_name,
                        "annotation": ast.unparse(statement.annotation),
                    }
                )
    return issues


def parse_schema(source: str, *, require_relations: bool = True) -> ParsedSchema:
    if not str(source).strip():
        raise ContractError("schema.py cannot be empty")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ContractError("schema.py has invalid Python syntax", line=exc.lineno, error=exc.msg) from exc

    class_nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    typing_imports = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "typing"
        for alias in node.names
    }
    required_typing_imports = {
        _node_name(statement.annotation.value)
        for node in class_nodes
        for statement in node.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.annotation, ast.Subscript)
        and isinstance(statement.annotation.value, ast.Name)
        and _node_name(statement.annotation.value) in {"List", "Optional"}
    }
    missing_typing_imports = sorted(required_typing_imports - typing_imports)
    if missing_typing_imports:
        raise ContractError(
            "Schema typing annotations require explicit imports",
            missing_imports=missing_typing_imports,
        )
    entity_base = next((node for node in class_nodes if node.name == "Entity"), None)
    if entity_base is None:
        raise ContractError("schema.py must define class Entity")
    base_fields = {
        statement.target.id: _node_name(statement.annotation)
        for statement in entity_base.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    }
    if base_fields.get("name") != "str":
        raise ContractError("class Entity must declare name: str")

    allowed_top_level = (ast.ImportFrom, ast.Import, ast.ClassDef)
    for node in tree.body:
        if not isinstance(node, allowed_top_level):
            raise ContractError("schema.py may contain only imports and class declarations", line=node.lineno)

    entity_nodes = [
        node
        for node in class_nodes
        if node.name != "Entity" and any(_node_name(base) == "Entity" for base in node.bases)
    ]
    if not entity_nodes:
        raise ContractError("schema.py must define at least one Entity subclass")
    names = [node.name for node in entity_nodes]
    if len(names) != len(set(names)):
        raise ContractError("schema.py contains duplicate entity class names")
    for name in names:
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
            raise ContractError("Entity class names must use PascalCase", entity=name)
    entity_names = set(names)

    declaration_issues = _declaration_issues(entity_nodes, entity_names)
    unsupported_field_issues = _unsupported_field_issues(entity_nodes, entity_names)
    if unsupported_field_issues:
        issues = [*unsupported_field_issues, *declaration_issues]
        raise ContractError(
            "Schema fields have unsupported types or incomplete declarations",
            issues=issues,
            repair_hint=(
                "Repair every listed annotation in one pass. Use primitive attributes or Optional primitive "
                "attributes. Use concrete entity types, Optional entity types, or List entity types for relations. "
                "Add every listed name or description in the same pass."
            ),
        )

    if len(declaration_issues) > 1:
        raise ContractError(
            "Schema declarations are incomplete; non-empty names and descriptions are required",
            issues=declaration_issues,
            repair_hint="Apply every listed declaration correction across the complete schema.",
        )

    entities: list[EntitySchema] = []
    relation_owners: dict[str, str] = {}
    for node in entity_nodes:
        entity_description = (ast.get_docstring(node) or "").strip()
        annotations = [
            statement
            for statement in node.body
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
        ]
        id_node = next((item for item in annotations if item.target.id == "_id"), None)
        id_type = _node_name(id_node.annotation) if id_node is not None else None
        if id_type not in SCHEMA_ID_TYPES:
            raise ContractError("Every entity must declare _id: str or _id: int", entity=node.name)
        seen_fields: set[str] = set()
        attributes: list[SchemaField] = []
        relations: list[SchemaField] = []
        field_descriptions = _attribute_docstrings(node)
        for statement in annotations:
            field_name = statement.target.id
            if field_name == "_id":
                continue
            if field_name in seen_fields:
                raise ContractError("Entity contains a duplicate field", entity=node.name, field=field_name)
            seen_fields.add(field_name)
            field = _field_from_annotation(field_name, statement.annotation, entity_names)
            if field.kind == "attribute":
                attributes.append(field)
            else:
                previous = relation_owners.get(field.name)
                if previous:
                    raise ContractError(
                        "Relation field names must be unique across schema",
                        relation=field.name,
                        first_owner=previous,
                        second_owner=node.name,
                    )
                relation_owners[field.name] = node.name
                relation_description = field_descriptions.get(field.name, "")
                if not relation_description:
                    raise ContractError(
                        "Every relation must declare a non-empty attribute docstring",
                        entity=node.name,
                        relation=field.name,
                    )
                relations.append(
                    SchemaField(
                        name=field.name,
                        kind=field.kind,
                        value_type=field.value_type,
                        optional=field.optional,
                        many=field.many,
                        description=relation_description,
                    )
                )
        name_field = next((field for field in attributes if field.name == "name"), None)
        if name_field is None or name_field.value_type != "str" or name_field.optional:
            raise ContractError("Every entity must declare name: str", entity=node.name)
        if not entity_description:
            raise ContractError("Every entity must declare a non-empty class docstring", entity=node.name)
        entities.append(
            EntitySchema(
                name=node.name,
                description=entity_description,
                id_type=str(id_type),
                attributes=tuple(attributes),
                relations=tuple(relations),
            )
        )
    parsed = ParsedSchema(tuple(entities))
    if require_relations and not parsed.relation_names:
        raise ContractError("schema.py must declare at least one relation field")
    return parsed


def _escape_docstring(text: str) -> str:
    return str(text or "").replace('"""', "'''").strip()


def _normalize_schema_blueprint(review: dict[str, Any]) -> dict[str, Any]:
    """Normalize review or model outline into entities plus flat relations."""
    if not isinstance(review, dict):
        raise ContractError("Schema review must be an object")
    raw_entities = review.get("entities")
    if not isinstance(raw_entities, list) or not raw_entities:
        raise ContractError("Schema review requires at least one entity")

    entities: list[dict[str, Any]] = []
    names: set[str] = set()
    nested_relations: list[dict[str, Any]] = []
    for item in raw_entities:
        if not isinstance(item, dict):
            raise ContractError("Every schema entity must be an object")
        name = str(item.get("name") or item.get("entity_type") or "").strip()
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name) or name in names:
            raise ContractError("Schema review has an invalid or duplicate entity name", entity=name)
        names.add(name)
        id_type = str(item.get("id_type") or item.get("entity_data_type") or "").strip()
        if not id_type:
            raise ContractError("Every schema entity requires id_type", entity=name)
        if id_type not in SCHEMA_ID_TYPES:
            raise ContractError("Schema entity ID type must be str or int", entity=name)
        description = str(item.get("description") or "").strip()
        if not description:
            raise ContractError("Every schema entity requires a non-empty description", entity=name)
        attributes = item.get("attributes")
        if attributes is None:
            attributes = []
        if not isinstance(attributes, list):
            raise ContractError("Schema entity attributes must be a list", entity=name)
        raw_nested = item.get("relations")
        if isinstance(raw_nested, list):
            for relation in raw_nested:
                if not isinstance(relation, dict):
                    raise ContractError("Every nested schema relation must be an object", entity=name)
                nested_relations.append({**relation, "head": name, "head_entity_type": name})
        entities.append(
            {
                "name": name,
                "id_type": id_type,
                "description": description,
                "attributes": attributes,
            }
        )

    raw_relations = review.get("relations")
    if raw_relations is None:
        raw_relations = []
    if not isinstance(raw_relations, list):
        raise ContractError("Schema relations must be a list")
    return {
        "entities": entities,
        "relations": [*list(raw_relations), *nested_relations],
        "names": names,
    }


def _relation_cardinality(item: dict[str, Any]) -> tuple[bool, bool]:
    """Return (many, optional) using review and outline field conventions."""
    if "many" in item:
        many = bool(item.get("many"))
        optional = bool(item.get("optional"))
        return many, optional
    # Historical review form: optional False -> List, optional True -> Optional single.
    optional = bool(item.get("optional"))
    return (not optional), optional


def schema_from_review(review: dict[str, Any], *, require_relations: bool = False) -> str:
    """Compile a structured entity/relation blueprint into schema Python source.

    Research schemas may omit relations. Downstream parse still enforces entity
    descriptions, relation descriptions, and supported field types.
    """
    blueprint = _normalize_schema_blueprint(review)
    entities = blueprint["entities"]
    names: set[str] = blueprint["names"]
    raw_relations = blueprint["relations"]

    relations_by_head: dict[str, list[dict[str, Any]]] = {}
    relation_names: set[str] = set()
    for item in raw_relations:
        if not isinstance(item, dict):
            raise ContractError("Every schema relation must be an object")
        name = str(item.get("name") or item.get("relation_type") or "").strip()
        head = str(item.get("head") or item.get("head_entity_type") or "").strip()
        tail = str(
            item.get("tail") or item.get("tail_entity_type") or item.get("target") or ""
        ).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in relation_names:
            raise ContractError("Schema review has an invalid or duplicate relation name", relation=name)
        if head not in names or tail not in names:
            raise ContractError(
                "Schema relation references an unknown entity",
                relation=name,
                head=head,
                tail=tail,
            )
        description = str(item.get("description") or "").strip()
        if not description:
            raise ContractError("Every schema relation requires a non-empty description", relation=name)
        many, optional = _relation_cardinality(item)
        relation_names.add(name)
        relations_by_head.setdefault(head, []).append(
            {
                "name": name,
                "tail": tail,
                "optional": optional,
                "many": many,
                "description": description,
            }
        )

    if require_relations and not relation_names:
        raise ContractError("Schema review requires at least one relation")

    needs_list = any(relation["many"] for group in relations_by_head.values() for relation in group)
    needs_optional = any(
        bool(attribute.get("optional"))
        for entity in entities
        for attribute in entity["attributes"]
        if isinstance(attribute, dict)
    ) or any(
        (not relation["many"]) and relation["optional"]
        for group in relations_by_head.values()
        for relation in group
    )
    imports = [name for name, needed in (("List", needs_list), ("Optional", needs_optional)) if needed]
    lines: list[str] = []
    if imports:
        lines.extend([f"from typing import {', '.join(imports)}", "", ""])
    lines.extend(["class Entity:", "    _id: str", "    name: str", "", ""])
    for entity in entities:
        lines.append(f"class {entity['name']}(Entity):")
        lines.append(f'    """{_escape_docstring(entity["description"])}"""')
        lines.append(f"    _id: {entity['id_type']}")
        lines.append("    name: str")
        for attribute in entity["attributes"]:
            if not isinstance(attribute, dict):
                raise ContractError("Every schema attribute must be an object", entity=entity["name"])
            field_name = str(attribute.get("name") or attribute.get("attribute") or "").strip()
            value_type = str(attribute.get("type") or attribute.get("attribute_data_type") or "").strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name) or value_type not in SCHEMA_PRIMITIVES:
                raise ContractError(
                    "Schema review has an invalid attribute",
                    entity=entity["name"],
                    field=field_name,
                )
            if field_name in {"_id", "name"}:
                continue
            annotation = f"Optional[{value_type}]" if attribute.get("optional") else value_type
            lines.append(f"    {field_name}: {annotation}")
        for relation in relations_by_head.get(entity["name"], []):
            tail = relation["tail"]
            if relation["many"]:
                annotation = f'List["{tail}"]'
            elif relation["optional"]:
                annotation = f'Optional["{tail}"]'
            else:
                annotation = f'"{tail}"'
            lines.append(f"    {relation['name']}: {annotation}")
            lines.append(f'    """{_escape_docstring(relation["description"])}"""')
        lines.append("")
        lines.append("")
    source = "\n".join(lines).rstrip() + "\n"
    parse_schema(source, require_relations=False)
    return source


def compile_schema_payload(payload: dict[str, Any], *, require_relations: bool = False) -> str:
    """Compile model schema_build payload into Python source.

    Preferred model payload:
      {"entities": [...], "relations": [...]}

    """
    if not isinstance(payload, dict):
        raise ContractError("Schema build payload must be an object")
    if payload.get("entities") is not None:
        return schema_from_review(payload, require_relations=require_relations)
    raise ContractError(
        "Schema build requires an entities and relations blueprint",
        repair_hint="Submit semantic entities and relations. Runtime compiles Python source.",
    )
