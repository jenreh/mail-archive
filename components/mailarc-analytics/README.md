# mailarc-analytics

What the archive can be asked once the ground truth is in the graph.

Co-recipient clusters, project topics and recurring text blocks — all derived,
all rebuildable, and all written to their own nodes. Keeping them apart from
`mailarc_core.archive` is the point: re-running an analysis must never be able
to overwrite a fact that came out of a message header.

The analyses are Cypher and SimHash, not inference. The one model this package
may talk to is an embedder, and it only ever adds a vector to a node that
already exists.

Empty until phase 5 fills `derived/`, `semantic/` and `queries/`.

## Rules

- Depends on `mailarc-core` alone.
- **No `mailarc-sync`.** Analysis runs after an import, not inside one.
- **No Reflex, no `appkit` UI package.** Results are values; rendering is the
  UI's job.
- **No `runic.rag`.** The graph is already exact — an LLM extraction would lay
  a probabilistic layer over ground truth.

`components/mailarc-core/tests/test_isolation.py` enforces the last two.
