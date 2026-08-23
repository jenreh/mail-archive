# Data model

Three stores, with a clean split of responsibility.

| Store | Holds | Rebuildable |
| --- | --- | --- |
| **Graph** | What a message *is* | From the blob store |
| **SQLite** | What we have *done* | Partly — the ledger, yes; accounts and credentials, no |
| **Blob store** | The original bytes | No. This is the archive |

## The graph — ground truth

![Graph ground truth](../diagrams/graph-model.svg)

Written by exactly one module:
[`mailarc_core.archive.writer`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-core/src/mailarc_core/archive/writer.py).
An analysis may read all of it and add nodes of its own beside it, but it never
edits what a provider actually sent. That separation is what makes a wrong
analysis a rerun instead of a restore.

Every property is read out of the message or computed from it deterministically,
so re-parsing the same bytes gives the same node.

### Nodes

| Node | Key | Why that key |
| --- | --- | --- |
| `Message` | canonical id | One node however often the mail arrives |
| `Address` | normalised address | `Bob@Example.COM` and `bob@example.com` are one human |
| `Thread` | `{account}:{thread_id}` | Two providers hand out thread ids from their own namespaces |
| `Label` | `{account}:{name}` | Always the provider's own label — a guess never becomes one |
| `Attachment` | sha256 of the file | The same file on twenty messages is one node |
| `Account` | SQLite row id | Ties a graph copy back to the mailbox it came from |

`Address.display_names` is a **list**, because the same address signs itself
differently in every message it sends. The writer appends a name it has not seen
before by *assignment*, not in-place mutation — runic tracks dirtiness through
the descriptor, so a mutated list would never reach the graph.

### Edges

| Edge | From → to | Carries |
| --- | --- | --- |
| `SENT_FROM` | Message → Address | |
| `SENT_TO` | Message → Address | |
| `COPIED_TO` | Message → Address | |
| `BLIND_COPIED_TO` | Message → Address | |
| `IN_THREAD` | Message → Thread | |
| `REPLIES_TO` | Message → Message | |
| `LABELED` | Message → Label | |
| `HAS_ATTACHMENT` | Message → Attachment | `filename`, `content_id`, `inline` |
| `ARCHIVED_FROM` | Message → Account | `provider_message_id`, `provider_thread_id`, `folder`, `uid`, `archived_at` |

Four separate edge types for the recipient roles rather than one edge with a
`role` property: RFC 5322 closes the set, so the co-recipient query walks them
without a property filter. `SENT_FROM` rather than `FROM`, because `FROM` is
awkward in several Cypher dialects.

`ARCHIVED_FROM` is where the anti-corruption layer shows up in the graph. The
same mail reaching two accounts is *one* `Message` node with two edges, so
everything that differs between the two copies — the provider's id, the folder,
the labels — sits on the edge or on the account, never on the message.

### The five analysis-bearing fields

Computed once, at import time, in
[`mail/parsing.py`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-core/src/mailarc_core/mail/parsing.py):

| Field | What it is |
| --- | --- |
| `subject_norm` | Reply prefixes and ticket tokens gone, lowercased, whitespace collapsed |
| `participant_key` | sha256 over the sorted set of everyone on the message |
| `simhash` | 64-bit SimHash over word shingles of `body_clean` |
| `refs` | Ticket tokens pulled out of the subject and the body |
| `body_clean` | The body without quoted predecessors, sign-off and legal footer |

Everything downstream trusts that parsing already did this. Nobody re-reads a
header later.

**`body_text` and `body_clean` are both needed and are not interchangeable.**
`body_text` is the full text and feeds full-text search. `body_clean` is what
gets hashed and embedded. Skip the cleaning and every message sharing a company
footer hashes alike, and the template analysis returns noise.

The cleaning rules are blunt heuristics over German and English conventions —
a reply intro, a `>` quote, a `--` separator, a sign-off, a legal boilerplate
opener. They cut a little too much rather than too little: a lost sentence costs
recall, a kept footer costs correctness.

### The SimHash sign problem

A SimHash uses all 64 bits. Every Cypher backend's integer is *signed* 64-bit,
so half of all messages carry a value the graph cannot hold.

`to_signed_64` / `to_unsigned_64` reinterpret via two's complement. The bit
pattern survives, which is all the template analysis needs — it compares Hamming
distances, never magnitudes.

### Canonical identity

```text
normalise(Message-ID)                                  when the sender sent one
sha256:<hash of  sent_at | from | subject | sha256(body)>   when they did not
```

The contract: importing the same mailbox twice creates zero new nodes and zero
new edges. That only works if the id comes from the *message*, not from the
provider.

RFC 5322 says a `Message-ID` is globally unique, so it wins whenever there is
one. Normalising it lowercases the domain half and keeps the local part's case —
RFC 5322 says the local part is significant there, unlike in an address, where
every real mail system disagrees.

The fallback hashes the *decoded* body, not the raw MIME part: a relay that
re-encodes quoted-printable as base64 changes the bytes on the wire but not the
text, and the id must survive that.

### Indexes

Declared on the model, created by `runic.migrate` — the `index` and `index_type`
arguments are declarations only.

```python
op.create_range_index(
    "Message", prop
)  # sent_at, subject_norm, simhash, participant_key
op.create_range_index("Address", "domain")
op.create_constraint("UNIQUE", "NODE", "Message", ["rfc_message_id"])
op.create_fulltext_index("Message", "subject", "body_text")
```

Two gotchas the baseline migration records:

- `create_constraint` creates its own index unconditionally, and FalkorDB
  rejects a second `CREATE INDEX` on an indexed attribute. So `rfc_message_id`
  gets no explicit range index — asking for both fails the migration.
- One full-text index covers **both** properties. The archive searches them
  together, and FalkorDB keeps one such index per label.

The vector index on `Message.embedding` is deliberately **not** in the baseline:
nothing wrote embeddings before phase 6, and an HNSW index costs memory from the
day it exists. Revision `5f4678dfc5a4` adds it — 768 dimensions, cosine, `M=16`,
`efConstruction=400`, `efRuntime=512` — and those five numbers are read back off
a running server by `tests/test_graph_migrations_vector_local.py`, because a
wrong one does not raise. FalkorDB accepts a vector of any other length, stores
it and silently declines to index it, so a job against a mismatched index
reports every message embedded and no search finds one. `efRuntime` cannot be
overridden at query time, which is why the migration's literal is the only
chance to set it: runic's default of 10 measured 14 % recall@10 where 512
measured 99 %.

The second revision adds the three the derived layer needs, and one on the
ground truth:

```python
op.create_range_index(label, "id")  # Group, Topic, Template
op.create_range_index("Message", "id")
```

Range indexes, not unique constraints, although each of those keys is unique by
construction. A constraint is checked on every write to the label, and these
nodes are deleted and rewritten wholesale on every rebuild — so it would charge
for a guarantee the id already gives. Without any index, though, each of the
`MERGE (:Group {id: ...})` statements a rebuild issues scans every node wearing
that label, and a rebuild issues one per finding.

`Message.id` is there for the read rather than the write. The baseline indexed
the four properties the analyses *filter* by and left the primary key alone,
because nothing walked the archive in id order until the derived reader did:
it pages the ground truth with `WHERE m.id > $after ... ORDER BY m.id`, which
is an index seek with the index and a full sort of every message without it,
once per page.

### A declaration-order trap

Class order in `archive/model.py` is load-bearing. runic resolves a node's
annotations *at declaration time*, so every type an annotation names has to
exist already.

`Message.replies_to` points at `Message`, which is not bound while the class
body is still running. It is therefore annotated `Any`, with the real type in
`target=`. A forward reference there does not merely fail for that field — it
aborts the whole resolution pass and **silently strips the datetime and vector
converters off every other field on the node**.

## The graph — derived

Written by exactly one module in the other direction:
[`mailarc_analytics.derived`](https://github.com/jenreh/mail-archive/tree/main/components/mailarc-analytics/src/mailarc_analytics/derived).
Everything here is an *answer*, not a fact, and every node is disposable —
`task graph:rebuild-derived` deletes all of it and computes it again. An
analysis bug therefore costs one run, never a restore.

### Nodes

| Node | Key | Holds |
| --- | --- | --- |
| `Group` | `participant_key` | `size`, `message_count`, `first_seen`, `last_seen` |
| `Topic` | `topic:<sha256 of its members>` | `label`, `method`, `score`, `message_count`, `first_seen`, `last_seen` |
| `Template` | `template:<simhash>:<direction>` | `sample_text`, `occurrences`, `automation_score`, `direction`, `first_seen`, `last_seen` |

The keys are hashes of the finding's own content, not ULIDs as first sketched.
A generated id would be new on every rebuild, which makes "run it twice and
nothing changes" unprovable — the whole derived layer would churn on each run
even when the archive had not moved.

### Edges

| Edge | From → to | Carries |
| --- | --- | --- |
| `CO_ADDRESSED` | Address → Address | `count`, `first_seen`, `last_seen` |
| `ADDRESSED_GROUP` | Message → Group | |
| `ABOUT` | Message → Topic | `score`, `method` |
| `INSTANCE_OF` | Message → Template | `distance` |

`CO_ADDRESSED` is undirected, and its `MERGE` pattern carries no arrow, so the
same pair handed in either order finds the one edge instead of growing a second.
Which way round it ended up stored is an accident of who was written to first —
so every read has to match it without an arrow too. Bcc'd addresses are
deliberately left out of it: a blind copy was written to *without* the other
recipients knowing, and an edge saying otherwise would be wrong about the one
thing Bcc means.

`ABOUT.method` is what keeps a suggestion from hardening into a fact. A
`method="ref"` cluster was drawn by a ticket token both messages carry; a
`method="embedding"` cluster is a guess — a nearest-neighbour pair above
`app_semantic_topic_similarity_min`, offered only where the five exact signals
left two messages unconnected, and only on an installation with an embedder
configured. It stays a
plain string rather than an enum on purpose — a graph written by a newer build
has to keep decoding in an older one.

### A derived node never becomes ground truth

`Label` comes from the provider, `Topic` comes from us, and nothing merges them.
A user may promote a `Topic` into a real `Label`; there is no way back. The
delete statements in the query catalogue are pinned to that rule at import time
— [`rebuild.py`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-analytics/src/mailarc_analytics/derived/rebuild.py)
matches each of the four against an exact shape and refuses to import if one
has been edited into something that could reach a `Message`.

## The relational store

![What the relational store holds](../diagrams/relational-schema.svg)

Six tables, in
[`database/entities.py`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-core/src/mailarc_core/database/entities.py).

| Table | Holds |
| --- | --- |
| `mail_accounts` | Which mailbox we sync, and where it stands |
| `mail_credentials` | What opens it, encrypted, opaque |
| `mail_sync_checkpoints` | How far a scope got, so the next run resumes |
| `mail_sync_jobs` | One unit of work a worker claims and reports on |
| `mail_archived_messages` | Provider ids already archived — a read model |
| `mail_failed_messages` | What we gave up on, and why |

Every enum-ish column is a short string, not a database enum. Adding a provider,
a job kind or a status is then a code change and never a migration.

### `mail_archived_messages` is a read model

It exists for exactly one query: *"of these hundred provider ids, which do I
already have?"*

The graph cannot answer that cheaply for a batch; a relational `IN (…)` can, in
one statement per listing page. Nothing but speed lives here, which is why the
table stays discardable — it can be rebuilt from the graph at any time.

Its `canonical_id` column is `Text` rather than a sized `VARCHAR`, on purpose: a
canonical id is either a normalised `Message-ID`, whose length nobody but the
sender controls, or the 71-character `sha256:` fallback. Any width small enough
to look tidy is one a real mailbox eventually overflows. SQLite ignores the
length anyway; PostgreSQL would have raised on the first message that arrived
without a `Message-ID`.

### `mail_failed_messages` is the other half of the ledger

Skipping is allowed. Silence is not. Every message the import drops leaves a row
here, so a permanent failure is countable rather than invisible.

There is no `except: pass` anywhere in the import path, and no place one would
fit — a `MailPermanentError` becomes a *value* that travels the same queue as a
success, and the same consumer writes both.

### SQLite specifics

`appkit_commons` exposes exactly one `DatabaseConfig.url` and hands it to both
the sync and the async engine. The application only ever opens async sessions,
so the configured URL names `aiosqlite`; Alembic and Reflex's `db_url` ask
`sync_database_url()` for the pysqlite variant.

Three pragmas are not optional:

| Pragma | Without it |
| --- | --- |
| `journal_mode=WAL` | A reader blocks on a writer |
| `foreign_keys=ON` | SQLite **silently ignores** `ON DELETE CASCADE` |
| `busy_timeout=5000` | A second writer raises `database is locked` |

They are registered on the SQLAlchemy `Engine` class, not on one engine.
appkit builds the sync and async engines lazily behind its own caches, and
Reflex builds a third — all against the one SQLite file this application owns.

## The blob store

Content-addressed and write-once. The file name *is* the sha256 of what is in
it.

```text
.state/mailstore/ab/cd/abcd…ef.eml
.state/mailstore/12/34/1234…56.bin
```

Two levels of fan-out by the first two byte pairs, so a million messages spread
over 65 536 leaf directories instead of one unlistable one.

Every write goes to a temporary file **in the destination directory** and lands
with `os.replace`. Same directory, because a rename is only atomic within one
filesystem and the system temp directory is routinely a different one. `fsync`
before the rename, so what lands is the whole content and not the empty file the
page cache had not written yet.

Nothing here ever overwrites: identical content means an identical name, and
different content cannot land on the same name. Write-once is not an
optimisation — it is the guarantee that a retried import cannot change what is
already on disk.

**Keeping the whole `.eml` matters more than it looks.** The graph holds a
capped, parsed rendering of a message; the bytes on disk are the message. A
parser fix can be replayed over the entire archive from here without asking a
provider for anything twice.
