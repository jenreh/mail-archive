"""The schema transform, which is the one place a provider quietly refuses.

``QueryIntent.model_json_schema()`` is not a schema either provider will take.
OpenAI's ``strict: true`` refuses an object without ``additionalProperties:
false`` and refuses one whose ``required`` omits a property; Ollama's ``format``
handles ``$ref`` unevenly. Both failures arrive as a 400 with a message about
the schema, which is a long way from the pydantic model that caused it — so
each rule is asserted here rather than discovered against a real endpoint.

:func:`~mailarc_analytics.nlq.model.strict_schema` is deliberately one function
shared by all three adapters. Two spellings would drift, and only the provider
whoever wrote the change happened to run would be tested.
"""

import json

from pydantic import BaseModel, ConfigDict

from mailarc_analytics.nlq.model import (
    Order,
    Period,
    QueryIntent,
    ResolvedQuery,
    strict_schema,
)

SCHEMA = strict_schema(QueryIntent)


class TestTheSchemaEveryProviderIsSent:
    def test_it_has_no_references_left_in_it(self) -> None:
        """A field typed as an enum makes pydantic emit one, and Ollama's
        ``format`` does not resolve them reliably."""
        rendered = json.dumps(SCHEMA)
        assert "$ref" not in rendered
        assert "$defs" not in rendered

    def test_every_object_refuses_what_it_did_not_declare(self) -> None:
        for node in _objects(SCHEMA):
            assert node["additionalProperties"] is False

    def test_every_object_requires_all_of_its_properties(self) -> None:
        """Strict mode has no optional property, which is why ``QueryIntent``
        spells "not answered" as an empty string or ``null`` rather than as a
        missing key."""
        for node in _objects(SCHEMA):
            assert node["required"] == list(node["properties"])

    def test_the_enums_arrive_as_their_own_values(self) -> None:
        assert SCHEMA["properties"]["period"]["enum"] == [one.value for one in Period]
        assert SCHEMA["properties"]["order"]["enum"] == [one.value for one in Order]

    def test_a_field_keeps_the_description_written_for_the_model(self) -> None:
        """Pydantic puts the *field's* description beside the reference and the
        referenced class's docstring inside it. Inlining naively keeps the
        docstring — which is written for a developer, Sphinx roles and all."""
        described = SCHEMA["properties"]["period"]["description"]
        assert "relative stretch of time the question names" in described
        assert ":func:" not in described

    def test_the_root_carries_no_docstring(self) -> None:
        """The system prompt says what the object is; the class docstring says
        it again in a dialect the model has no use for."""
        assert "description" not in SCHEMA

    def test_nothing_a_provider_ignores_is_sent(self) -> None:
        """``title`` restates the field name it sits beside, and ``default`` is
        meaningless once everything is required — OpenAI lists it among the
        keywords strict mode does not support."""
        rendered = json.dumps(SCHEMA)
        assert '"title"' not in rendered
        assert '"default"' not in rendered

    def test_a_tri_state_stays_nullable(self) -> None:
        """ "either" is a real answer for the attachment filter, and a plain
        boolean could not say it."""
        assert SCHEMA["properties"]["has_attachments"]["anyOf"] == [
            {"type": "boolean"},
            {"type": "null"},
        ]

    def test_it_is_small_enough_to_send_with_every_question(self) -> None:
        """Not a style rule: it rides on every request, and the descriptions
        are the part worth paying for."""
        assert len(json.dumps(SCHEMA)) < 4_000


class TestTheTransformItself:
    def test_a_nested_model_is_inlined_rather_than_referenced(self) -> None:
        class Inner(BaseModel):
            model_config = ConfigDict(extra="forbid")
            name: str = ""

        class Outer(BaseModel):
            model_config = ConfigDict(extra="forbid")
            inner: Inner = Inner()

        found = strict_schema(Outer)
        assert found["properties"]["inner"]["properties"]["name"]["type"] == "string"
        assert "$defs" not in json.dumps(found)

    def test_a_nested_object_gets_the_rules_too(self) -> None:
        class Inner(BaseModel):
            name: str = ""

        class Outer(BaseModel):
            inner: Inner = Inner()

        inner = strict_schema(Outer)["properties"]["inner"]
        assert inner["additionalProperties"] is False
        assert inner["required"] == ["name"]


class TestTheValuesThatCrossBackToThePage:
    def test_an_intent_is_frozen(self) -> None:
        """It crosses a state lock, so it may not be edited on the way."""
        assert QueryIntent.model_config["frozen"] is True

    def test_a_resolved_query_is_frozen(self) -> None:
        assert ResolvedQuery.model_config["frozen"] is True

    def test_an_intent_refuses_a_field_it_does_not_know(self) -> None:
        """A model inventing a field has misread the schema, and what else it
        sent is not to be trusted either."""
        assert QueryIntent.model_config["extra"] == "forbid"

    def test_no_period_is_the_empty_string(self) -> None:
        """So "the question named none" and "the question named this one" are
        the same field rather than a field and a flag."""
        assert Period.NONE.value == ""


def _objects(node: object) -> list[dict]:
    """Every object node in a schema, root included."""
    found: list[dict] = []
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            found.append(node)
        for value in node.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for one in node:
            found.extend(_objects(one))
    return found
