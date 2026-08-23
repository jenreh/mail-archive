"""vector index on message embedding

Revision ID: 5f4678dfc5a4
Revises: 53fcf8d0fe56
Create Date: 2026-08-22 18:33:31.335069+00:00

The index the baseline deliberately left out. Its docstring says why — nothing
wrote embeddings before phase 6 and an HNSW index costs memory from the day it
exists — and this is the day. Measured on the vendored FalkorDB with five
thousand messages: the property alone costs 1.17x the raw floats, and the index
costs another full copy of every vector on top, so at this dimension a hundred
thousand messages is roughly 0.73 GB of graph memory that did not exist before.

**The trap §7.4 names, spelled out.** This index is migrated to ONE dimension.
FalkorDB accepts a vector of any other length without complaint and silently
declines to index it — measured: writing a length-2 vector into a dimension-4
index leaves ``numDocuments`` unchanged and ``indexingFailures`` at zero. So a
changed embedder does not fail, it *disappears*: the job reports every message
embedded and the search finds none of them. That is why
``Message.embedding_model`` sits on the node — it makes the change detectable
and the recomputation targeted — and why
:func:`mailarc_analytics.semantic.indexing.verify` reads the live index's
dimension before a run writes anything, rather than trusting the setting.
Changing :data:`DIMENSION` therefore needs a new revision that drops and
recreates the index **and** an embed job that recomputes every vector.

Reversible, and it really is: a downgrade drops the index, an upgrade builds it
again from the vectors already stored on the nodes, and nothing has to be
re-embedded. Verified against a live server, upgrade → downgrade → upgrade,
with ``CALL DB.INDEXES()`` read after each — the vectors were findable again
afterwards. What it must not do is run twice: FalkorDB refuses a second
``CREATE VECTOR INDEX`` on an indexed attribute with "Attribute 'embedding' is
already indexed", which runic's revision tracking is what prevents.

All index kinds share one structure per label and coexist. ``Message`` already
carries range indexes on five properties and one full-text index over
``subject`` and ``body_text``; adding this one leaves both working — checked on
a live server, not assumed.
"""

from datetime import datetime
from typing import Any

message = "vector index on message embedding"
create_date = datetime.fromisoformat("2026-08-22T18:33:31.335069+00:00")

revision = "5f4678dfc5a4"
down_revision = "53fcf8d0fe56"
branch_labels: list[str] = []
depends_on: list[str] = []
irreversible = False
snapshot = False

LABEL = "Message"
PROPERTY = "embedding"

DIMENSION = 1536
"""Floats per vector — and the number that cannot be changed in place.

Must equal :attr:`mailarc_analytics.semantic.config.SemanticConfig.dimension`.
1536 and not 768 because only 1536 is reachable from both providers: the local
``nomic-embed-text`` is 768 natively and needs no account, while OpenAI's
``text-embedding-3-small`` is 1536 natively and can be *asked* for 768 through
its ``dimensions`` parameter — which the adapter always does, for exactly this
reason. Choosing 768 would have made the OpenAI path impossible and
halved the memory (measured 7.3 KB per message against 14 KB).
"""

SIMILARITY = "cosine"
"""How two vectors are compared. ``cosine`` or ``euclidean`` are the only two
FalkorDB accepts — ``l2``, ``dot`` and ``ip`` are refused by the server itself,
not merely by runic. Cosine, because both embedding models are trained to be
compared that way and because it ignores magnitude.
"""

EF_RUNTIME = 512
"""How many candidates a search keeps in flight. **Not runic's default of 10.**

There is no query-time override — ``db.idx.vector.queryNodes`` takes exactly
four arguments and rejects an options map — so this literal is the only chance
to set it, and 10 is not a tuning choice but a broken one. Measured recall@10
against exact cosine over a clustered 768-dimensional corpus of five thousand
vectors: 14 % at ef=10, 30 % at 32, 45 % at 64, 65 % at 128, 83 % at 256, 99 %
at 512 — for 0.6 ms against 2.2 ms per search. Two milliseconds is nothing
beside the HTTP call that produced the query vector.

It also decides how much over-fetching costs. FalkorDB searches with
``ef = max(efRuntime, k)``, so at ef=10 a caller asking for the ten nearest and
filtering them down to three gets a bad ten to begin with; at 512 an ordinary
small-k search is already good and the over-fetch only has to pay for the rows
a filter drops.
"""

EF_CONSTRUCTION = 400
"""Candidates kept while *building* the graph, against runic's default of 200.

Roughly eight points of recall for about 28 % more index build time, paid once
per message at insert. Worth it here because the number is chosen once and the
insert is already dominated by the embedding call in front of it.
"""

M = 16
"""Links per node in the HNSW graph — runic's default, kept.

Measured at 8, 16 and 32 with less than 2 % difference in memory: at these
dimensions the vectors dominate and the links do not. No reason to move it.
"""


def upgrade(op: Any) -> None:
    op.create_vector_index(
        LABEL,
        PROPERTY,
        DIMENSION,
        SIMILARITY,
        m=M,
        ef_construction=EF_CONSTRUCTION,
        ef_runtime=EF_RUNTIME,
    )


def downgrade(op: Any) -> None:
    op.drop_vector_index(LABEL, PROPERTY)
