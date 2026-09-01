"""What the archive can be asked, once the ground truth is in the graph.

Derived nodes are written here and only here, deliberately apart from the
archive writer, so that re-running an analysis can never overwrite a fact taken
from a message header. Nothing in this package is invented by a model:
co-recipients, topics and templates come out of Cypher and SimHash. An embedder
is the one model allowed near it, and it only ever adds a vector.

:mod:`mailarc_analytics.derived` holds the analyses and everything they agree
on; the names below belong to it and are re-exported so the application can say
``from mailarc_analytics import AnalyticsConfig, rebuild_derived`` — which,
with a ``GraphConfig``-backed session, is the whole of what the ``derive`` job
needs.

:mod:`mailarc_analytics.queries` holds the statements and the one façade that
runs them. The *statements* are reached as a package — ``from
mailarc_analytics.queries import catalog`` — because pulling one out by name
loses the module docstring that says what may be done with it.
:class:`~mailarc_analytics.queries.reports.AnalyticsReader` and the rows it
answers with are ordinary values and are re-exported below, so a page can say
``from mailarc_analytics import AnalyticsReader`` the way it already says
``from mailarc_core import ArchiveReader``.

:mod:`mailarc_analytics.semantic` is reached by name and is deliberately **not**
re-exported here — ``from mailarc_analytics.semantic import SemanticSearch``.
It is the one part of this package where a model is involved at all, and
keeping it a named import means that everything a bare ``import
mailarc_analytics`` offers is still exact: a ticket token, a thread, a subject,
a hash. The embedder is opt-in in the configuration and opt-in in the import
statement, which is the same decision written down twice.
"""

from mailarc_analytics.derived import (
    About,
    AddressedGroup,
    AnalyticsConfig,
    CoAddressed,
    CoAddressedPair,
    CorrespondentFindings,
    DerivedCounts,
    DisjointSet,
    Group,
    GroupFacts,
    InstanceOf,
    MessageFacts,
    ProgressHook,
    RebuildProgress,
    RebuildStage,
    SIGNAL_WEIGHTS,
    SimilarityEdge,
    Template,
    TemplateCluster,
    TemplateDirection,
    TemplateGroup,
    TemplateGrouping,
    TemplateMember,
    Topic,
    TopicCluster,
    TopicFindings,
    TopicMember,
    TopicSignal,
    automation_score,
    band_keys,
    build_correspondents,
    build_topics,
    count_messages,
    count_unidentified,
    describe_templates,
    group_templates,
    read_account_addresses,
    read_bodies,
    read_facts,
    rebuild_derived,
    template_id,
    topic_id,
    write_correspondents,
    write_templates,
    write_topics,
)
from mailarc_analytics.queries import (
    AGREEMENT_LIMIT,
    REPORT_LIMIT,
    AnalyticsReader,
    ArchivedDay,
    ArchiveTotals,
    CoAddressedAgreement,
    CoAddressedRow,
    ComparedPair,
    CoRecipientRow,
    GroupRow,
    TemplateRow,
    TopicRow,
)

__all__ = [
    "AGREEMENT_LIMIT",
    "REPORT_LIMIT",
    "SIGNAL_WEIGHTS",
    "About",
    "AddressedGroup",
    "AnalyticsConfig",
    "AnalyticsReader",
    "ArchiveTotals",
    "ArchivedDay",
    "CoAddressed",
    "CoAddressedAgreement",
    "CoAddressedPair",
    "CoAddressedRow",
    "CoRecipientRow",
    "ComparedPair",
    "CorrespondentFindings",
    "DerivedCounts",
    "DisjointSet",
    "Group",
    "GroupFacts",
    "GroupRow",
    "InstanceOf",
    "MessageFacts",
    "ProgressHook",
    "RebuildProgress",
    "RebuildStage",
    "SimilarityEdge",
    "Template",
    "TemplateCluster",
    "TemplateDirection",
    "TemplateGroup",
    "TemplateGrouping",
    "TemplateMember",
    "TemplateRow",
    "Topic",
    "TopicCluster",
    "TopicFindings",
    "TopicMember",
    "TopicRow",
    "TopicSignal",
    "automation_score",
    "band_keys",
    "build_correspondents",
    "build_topics",
    "count_messages",
    "count_unidentified",
    "describe_templates",
    "group_templates",
    "read_account_addresses",
    "read_bodies",
    "read_facts",
    "rebuild_derived",
    "template_id",
    "topic_id",
    "write_correspondents",
    "write_templates",
    "write_topics",
]
