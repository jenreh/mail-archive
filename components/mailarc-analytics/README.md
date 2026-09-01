# mailarc-analytics

What the archive can be asked once the ground truth is in the graph.

Co-recipient clusters, project topics and recurring text blocks — all derived,
all rebuildable, and all written to their own nodes. Keeping them apart from
`mailarc_core.archive` is the point: re-running an analysis must never be able
to overwrite a fact that came out of a message header.

The analyses are Cypher and SimHash, not inference. The one model this package
may talk to is an embedder, and it only ever adds a vector to a node that
already exists.

## Layout

```
derived/
  model.py            the Group, Topic and Template nodes, the four derived
                      edges, the value objects the analyses pass around and
                      the two deterministic id functions. No I/O.
  config.py           AnalyticsConfig — every threshold that decides what
                      counts as a finding.
  partition.py        union-find. Both A2 and A3 are connected components,
                      and neither of them may import the other.
  reader.py           the one place the derived layer reads ground truth, and
                      the one place a stored SimHash is converted to unsigned.
  writes.py           the batched UNWIND all three write halves run.
  correspondents.py   A1 — who gets written to together (CO_ADDRESSED), and
                      which circles keep writing (Group).
  topics.py           A2 — five exact signals into one similarity graph, cut
                      into connected components and written as Topic.
  templates.py        A3 — SimHash LSH over body_clean, sent and received
                      apart, written as Template with an automation score.
  rebuild.py          delete all four derived types and compute them again.
                      The entry point the derive job calls.
queries/
  catalog.py          every Cypher statement the whole component runs, named
                      and parameterised. Nothing outside builds one.
  model.py            what a report answers with — frozen value objects, plus
                      the cross-check verdict and the floor rule that decides
                      which pairs it may rule on at all.
  rows.py             reading a raw result set back past runic's mapper: a
                      header, a list of lists, and one decoder per column type.
  reports.py          AnalyticsReader, the read façade. Takes numbers, never a
                      statement.
semantic/
  config.py           SemanticConfig — provider (default `none`), model,
                      dimension, base URL, key, batch and page sizes.
  ports.py            EmbedderPort. The one port here, and it earns its place:
                      two implementations exist from day one.
  embedder.py         OllamaEmbedder and OpenAIEmbedder over httpx, plus
                      build_embedder(), which answers None when nothing is
                      configured. No httpx exception leaves the module.
  model.py            frozen values: a search request, a hit, a result, a
                      batch, a run, and the coverage every semantic answer
                      carries. No I/O.
  errors.py           the sentences a user reads when a vector question cannot
                      be answered, each naming the setting that fixes it.
  search.py           the two search paths: full text (always works) and KNN
                      (needs an embedder, an index and a completed job).
  indexing.py         the embed job's half: page the archive, embed, write the
                      vectors back beside the ground truth.
```

Every analysis is a **pure compute half and a thin write half**: the compute
half is a function over `MessageFacts` that touches no graph, so what decides
whether an analysis is right is tested without a server.

`semantic/` is reached by name — `from mailarc_analytics.semantic import …` —
and is deliberately not re-exported at the package root, so everything a bare
`import mailarc_analytics` offers is still exact.

## Embeddings are optional, and saying so is a feature

`app_semantic_provider` defaults to `none`. Without an embedder A1–A3 run in
full and exactly two things are missing: semantic search, and A2's sixth
signal. That is what keeps the desktop application free of prerequisites.

**With no embedder, a semantic search raises rather than returning nothing.**
An empty list is a valid answer to a search, so a user who gets one concludes
their archive holds nothing on the subject and stops looking. The message names
the setting to change and says which searches still work.

**The vector index is migrated to one fixed dimension** (768, `cosine`,
`efRuntime` 512 — `graph_migrations/versions/5f4678dfc5a4_*`). FalkorDB accepts
a vector of any other length, stores it, and silently declines to index it: no
exception, no log line, no `indexingFailures`. So a changed embedder does not
fail, it disappears. Three things stand against that — `Message.embedding_model`
on the node makes the change detectable, `EmbeddingBatch.assemble` refuses a
vector of the wrong length before the write, and `indexing.verify()` compares
the configured dimension against the *live* index before a job writes anything.
Changing the dimension needs a new migration **and** a re-embedding run.

Every semantic answer carries its coverage (`embedded / total`), because a KNN
over a half-embedded archive returns a short, entirely plausible result set
that looks exactly like a complete search over a small one.

## What is derived, and how it is keyed

| Node | key | written from |
| --- | --- | --- |
| `Group` | the message's `participant_key` | A1 |
| `Topic` | sha256 over its members' sorted canonical ids | A2 |
| `Template` | its representative's unsigned SimHash plus the direction | A3 |

```
(Address)-[:CO_ADDRESSED {count, first_seen, last_seen}]->(Address)  # ordered pair
(Message)-[:ADDRESSED_GROUP]->(Group)
(Message)-[:ABOUT {score, method}]->(Topic)
(Message)-[:INSTANCE_OF {distance}]->(Template)
```

**The ids are deterministic, and the spec's ULID is not used.** A random key
would make `task graph:rebuild-derived` write a different graph on every run,
and "twice changes nothing" is the property phase 5 has to have. A topic *is*
the set of messages in it, so the digest of that set is the only key under
which a rebuild is a no-op.

**`CO_ADDRESSED` is undirected in meaning and directed in storage,** because
every Cypher edge is and FalkorDB refuses an undirected `MERGE` outright.
Exactly one edge is written per unordered pair, smaller address id first —
`CoAddressedPair` orders it, so the invariant is enforced rather than agreed —
and every read has to match it without an arrow.

## Rules

- Depends on `mailarc-core` plus `httpx`, the one thing the embedder needs.
  Never a sibling component.
- **No `mailarc-sync`.** Analysis runs after an import, not inside one.
- **No Reflex, no `appkit` UI package.** Results are values; rendering is the
  UI's job.
- **No `runic.rag`.** The graph is already exact — an LLM extraction would lay
  a probabilistic layer over ground truth.
- **No free Cypher.** Every statement is a named constant in
  `queries/catalog.py` and every caller value is a bound parameter. That covers
  Cypher; the full-text path is a **second** query language — RediSearch has
  `|`, `-`, `@field:` and a syntax error on a lone `(` — so a caller's words are
  reduced to words in `semantic/search.py` before they reach it.
- **An embedder may only add a vector.** `semantic/` is the one place a model
  is involved at all, and the deterministic analyses may not import it;
  `tests/test_analytics_package.py` reads the imports and checks.

`components/mailarc-core/tests/test_isolation.py` enforces the middle two.
