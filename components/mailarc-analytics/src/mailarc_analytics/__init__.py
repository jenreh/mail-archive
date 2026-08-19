"""What the archive can be asked, once the ground truth is in the graph.

Derived nodes are written here and only here, deliberately apart from the
archive writer, so that re-running an analysis can never overwrite a fact taken
from a message header. Nothing in this package is invented by a model:
co-recipients, topics and templates come out of Cypher and SimHash. An embedder
is the one model allowed near it, and it only ever adds a vector.

Empty until phase 5 fills ``derived/``, ``semantic/`` and ``queries/``.
"""
