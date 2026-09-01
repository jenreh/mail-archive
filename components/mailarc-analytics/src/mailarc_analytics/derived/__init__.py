"""The derived layer: what the archive means, written beside what it says.

Three analyses, all of them exact. Who gets written to together, which
messages belong to one piece of work, which text gets retyped every month —
each answered from properties the import already computed, none of them
guessed. What they write goes to labels of its own, so ``task
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
    The runic OGM nodes and edges, the value objects the analyses pass around
    and the two deterministic id functions. No I/O and no algorithm.
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
    The batched ``UNWIND`` all three write halves run.
``correspondents``, ``topics``, ``templates``
    A1, A2 and A3.
``rebuild``
    Delete all four derived types and compute them again. The entry point the
    ``derive`` job calls, inside a thread, because runic blocks.
"""

from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.correspondents import (
    build_correspondents,
    write_correspondents,
)
from mailarc_analytics.derived.model import (
    About,
    AddressedGroup,
    CoAddressed,
    CoAddressedPair,
    CorrespondentFindings,
    DerivedCounts,
    Group,
    GroupFacts,
    InstanceOf,
    MessageFacts,
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
)
from mailarc_analytics.derived.rebuild import ProgressHook, rebuild_derived
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
    "CorrespondentFindings",
    "DerivedCounts",
    "DisjointSet",
    "Group",
    "GroupFacts",
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
    "Topic",
    "TopicCluster",
    "TopicFindings",
    "TopicMember",
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
