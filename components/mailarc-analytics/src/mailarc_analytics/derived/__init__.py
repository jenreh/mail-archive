"""The derived layer: what the archive means, written beside what it says.

Nine analyses, all of them exact. Who gets written to together, which
messages belong to one piece of work, which text gets retyped every month, who
forms a circle, which mail matters and what a topic is about — each answered
from properties the import already computed or from a graph procedure reading
them, none of them guessed. What they write goes to labels of its own, so ``task
graph:rebuild-derived`` can delete the lot and recompute it, and a bug in an
analysis costs one run instead of a restore.

Every analysis is a **pure compute half and a thin write half**. The compute
half is a function over :class:`MessageFacts` that returns value objects and
touches no graph, so the planted-fixture tests that decide whether an analysis
is right run without a server. The write half is a handful of ``MERGE``
statements out of :mod:`mailarc_analytics.queries.catalog`, so what needs a
server is only whether the rows arrive.

One module per concern, layered so nothing points back up:

``model``
    The runic OGM nodes and edges, the vocabularies and the three
    deterministic id functions. No I/O and no algorithm.
``findings``
    The value objects the four newer analyses pass between their halves —
    ``model``'s other half, split off at the thousand-line limit and
    re-exported from it.
``partition``
    Union-find. Both A2 and A3 are connected components, and neither of them
    may import the other.
``config``
    ``AnalyticsConfig`` — every threshold that decides what counts as a
    finding.
``reader``
    The one place the derived layer reads ground truth, and the one place a
    stored SimHash is converted back to unsigned.
``writes``
    The batched ``UNWIND`` every write half runs, in its two shapes — a
    ``MERGE`` over rows, and the ``SET`` the two ground-truth properties take.
``correspondents``, ``topics``, ``templates``
    A1, A2 and A3.
``algorithms``
    The capability probe and the guard around FalkorDB's ``algo.*``
    procedures. The only module that knows a procedure can refuse a graph.
``centrality``, ``conversations``, ``communities``, ``keywords``,
``importance``, ``suggestions``
    §5.2's six stages, each one a pure compute half and a thin write half like
    the three above it. ``centrality`` is power iteration in **Python** and
    not ``algo.pageRank``, because ``CO_ADDRESSED`` is stored with the smaller
    id first and a PageRank over that arrow would rank the alphabet.
``rebuild``
    Delete every derived type and compute them all again. The entry point the
    ``derive`` job calls, inside a thread, because runic blocks.
"""

from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.correspondents import (
    build_correspondents,
    write_correspondents,
)
from mailarc_analytics.derived.centrality import (
    weighted_pagerank,
    write_address_ranks,
)
from mailarc_analytics.derived.communities import (
    build_communities,
    write_communities,
)
from mailarc_analytics.derived.conversations import conversation_edges
from mailarc_analytics.derived.importance import score_messages, write_importance
from mailarc_analytics.derived.keywords import topic_keywords, write_keywords
from mailarc_analytics.derived.findings import (
    CommunityFacts,
    CommunityFindings,
    Grouping,
    GroupingKind,
    ImportanceScore,
    MessageSignals,
    Suggestion,
)
from mailarc_analytics.derived.model import (
    About,
    AddressedGroup,
    CoAddressed,
    CoAddressedPair,
    Community,
    CorrespondentFindings,
    DerivedCounts,
    Group,
    GroupFacts,
    InCircle,
    InstanceOf,
    MemberOf,
    MessageFacts,
    RebuildProgress,
    RebuildStage,
    SIGNAL_WEIGHTS,
    SimilarityEdge,
    Suggested,
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
    community_id,
    template_id,
    topic_id,
)
from mailarc_analytics.derived.partition import DisjointSet
from mailarc_analytics.derived.reader import (
    count_messages,
    count_unidentified,
    read_account_addresses,
    read_bodies,
    read_facts,
    read_replies,
    read_signals,
    read_tagged,
    read_texts,
)
from mailarc_analytics.derived.rebuild import ProgressHook, rebuild_derived
from mailarc_analytics.derived.suggestions import suggest, write_suggestions
from mailarc_analytics.derived.templates import (
    automation_score,
    band_keys,
    describe_templates,
    group_templates,
    write_templates,
)
from mailarc_analytics.derived.topics import build_topics, write_topics

__all__ = [
    "SIGNAL_WEIGHTS",
    "About",
    "AddressedGroup",
    "AnalyticsConfig",
    "CoAddressed",
    "CoAddressedPair",
    "Community",
    "CommunityFacts",
    "CommunityFindings",
    "CorrespondentFindings",
    "DerivedCounts",
    "DisjointSet",
    "Group",
    "GroupFacts",
    "Grouping",
    "GroupingKind",
    "ImportanceScore",
    "InCircle",
    "InstanceOf",
    "MemberOf",
    "MessageFacts",
    "MessageSignals",
    "ProgressHook",
    "RebuildProgress",
    "RebuildStage",
    "SimilarityEdge",
    "Suggested",
    "Suggestion",
    "Template",
    "TemplateCluster",
    "TemplateDirection",
    "TemplateGroup",
    "TemplateGrouping",
    "TemplateMember",
    "Topic",
    "TopicCluster",
    "TopicFindings",
    "TopicMember",
    "TopicSignal",
    "automation_score",
    "band_keys",
    "build_communities",
    "build_correspondents",
    "build_topics",
    "community_id",
    "conversation_edges",
    "count_messages",
    "count_unidentified",
    "describe_templates",
    "group_templates",
    "read_account_addresses",
    "read_bodies",
    "read_facts",
    "read_replies",
    "read_signals",
    "read_tagged",
    "read_texts",
    "rebuild_derived",
    "score_messages",
    "suggest",
    "template_id",
    "topic_id",
    "topic_keywords",
    "weighted_pagerank",
    "write_address_ranks",
    "write_communities",
    "write_correspondents",
    "write_importance",
    "write_keywords",
    "write_suggestions",
    "write_templates",
    "write_topics",
]
