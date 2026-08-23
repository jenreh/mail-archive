"""The named, parameterised Cypher this project is allowed to run — and the one
façade that runs it.

``catalog``
    Every statement the derived layer runs, plus :data:`~catalog.CATALOG`, the
    name-to-statement mapping a test iterates, and
    :func:`~catalog.parameters_of`, which reads a statement's parameters off
    its text.
``rows``
    The other half of the calling convention: raw Cypher goes past runic's
    mapper, so a result set comes back as a header and a list of lists with
    every value in whatever shape the driver made of it.
``model``
    What a report answers with — frozen value objects, one per statement's
    columns, plus the cross-check's verdict.
``reports``
    :class:`~mailarc_analytics.queries.reports.AnalyticsReader`, the read
    façade a page asks. It takes numbers and never a statement.

The rule all four exist for: nothing outside ``catalog`` composes Cypher.
Caller input arrives as a bound ``$parameter``, never as a formatted string, so
no address, subject or label can change what a statement does. Phase 6's MCP
server serves a model from here for exactly that reason, and
:class:`~mailarc_analytics.queries.reports.AnalyticsReader` is the shape that
rule takes when the caller is a page rather than an analysis.

The statements stay a *package* import — ``from mailarc_analytics.queries
import catalog`` — because pulling one out by name loses the module docstring
that says what may be done with it. The reader and its rows are ordinary
values and are re-exported like any other.
"""

from mailarc_analytics.queries.catalog import (
    ACCOUNT_ADDRESSES,
    CATALOG,
    CO_RECIPIENTS,
    COUNT_CO_ADDRESSED,
    COUNT_GROUPS,
    COUNT_MESSAGES,
    COUNT_NEEDING_EMBEDDING,
    COUNT_TEMPLATES,
    COUNT_TOPICS,
    COUNT_UNIDENTIFIED,
    DELETE_CO_ADDRESSED,
    DELETE_GROUPS,
    DELETE_TEMPLATES,
    DELETE_TOPICS,
    FULLTEXT_MESSAGES,
    MERGE_ABOUT,
    MERGE_ADDRESSED_GROUP,
    MERGE_CO_ADDRESSED,
    MERGE_GROUPS,
    MERGE_INSTANCE_OF,
    MERGE_TEMPLATES,
    MERGE_TOPICS,
    MESSAGE_BODIES,
    MESSAGE_PROPERTIES,
    MESSAGE_RELATIONS,
    MESSAGES_NEEDING_EMBEDDING,
    RECURRING_GROUPS,
    SEMANTIC_NEIGHBOURS,
    TOP_CO_ADDRESSED,
    TOP_TEMPLATES,
    TOPIC_BREAKDOWN,
    VECTOR_COVERAGE,
    VECTOR_INDEX_OPTIONS,
    WRITE_EMBEDDINGS,
    as_graph_datetime,
    parameters_of,
)
from mailarc_analytics.queries.model import (
    ArchiveTotals,
    CoAddressedAgreement,
    CoAddressedRow,
    CoRecipientRow,
    ComparedPair,
    GroupRow,
    TemplateRow,
    TopicRow,
)
from mailarc_analytics.queries.reports import (
    AGREEMENT_LIMIT,
    REPORT_LIMIT,
    AnalyticsReader,
)
from mailarc_analytics.queries.rows import (
    as_datetime,
    as_float,
    as_int,
    as_text,
    rows_of,
)

__all__ = [
    "ACCOUNT_ADDRESSES",
    "AGREEMENT_LIMIT",
    "CATALOG",
    "COUNT_CO_ADDRESSED",
    "COUNT_GROUPS",
    "COUNT_MESSAGES",
    "COUNT_NEEDING_EMBEDDING",
    "COUNT_TEMPLATES",
    "COUNT_TOPICS",
    "COUNT_UNIDENTIFIED",
    "CO_RECIPIENTS",
    "DELETE_CO_ADDRESSED",
    "DELETE_GROUPS",
    "DELETE_TEMPLATES",
    "DELETE_TOPICS",
    "FULLTEXT_MESSAGES",
    "MERGE_ABOUT",
    "MERGE_ADDRESSED_GROUP",
    "MERGE_CO_ADDRESSED",
    "MERGE_GROUPS",
    "MERGE_INSTANCE_OF",
    "MERGE_TEMPLATES",
    "MERGE_TOPICS",
    "MESSAGES_NEEDING_EMBEDDING",
    "MESSAGE_BODIES",
    "MESSAGE_PROPERTIES",
    "MESSAGE_RELATIONS",
    "RECURRING_GROUPS",
    "REPORT_LIMIT",
    "SEMANTIC_NEIGHBOURS",
    "TOPIC_BREAKDOWN",
    "TOP_CO_ADDRESSED",
    "TOP_TEMPLATES",
    "VECTOR_COVERAGE",
    "VECTOR_INDEX_OPTIONS",
    "WRITE_EMBEDDINGS",
    "AnalyticsReader",
    "ArchiveTotals",
    "CoAddressedAgreement",
    "CoAddressedRow",
    "CoRecipientRow",
    "ComparedPair",
    "GroupRow",
    "TemplateRow",
    "TopicRow",
    "as_datetime",
    "as_float",
    "as_graph_datetime",
    "as_int",
    "as_text",
    "parameters_of",
    "rows_of",
]
