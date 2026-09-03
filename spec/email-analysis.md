# Spec: E-Mail-Analyse — Projekt-Tags, Wichtigkeit, Graph-Algorithmen, Graph-Explorer

> **Status:** Entwurf zur Freigabe.
> **Quelle:** Ergebnis der Planungssitzung vom 2026-09-02; Entscheidungen in §1.1 sind mit dem Nutzer abgestimmt.
> **Umsetzbar ohne Vorwissen:** Alle Repo-Fakten, verifizierten APIs und Entscheidungen stehen hier. Was noch zu verifizieren ist, steht als Verifikationsschritt.

---

## 1. Ziel und Ausgangslage

Das Archiv beantwortet heute drei Fragen (A1 Ko-Adressaten, A2 Topics, A3 Vorlagen) über `mailarc-analytics`, zeigt sie als Tabellen auf `/insights` und liefert sie über MCP. Es fehlt:

| # | Frage | Antwort dieser Spec |
| --- | --- | --- |
| B1 | Welche Mails gehören zum **gleichen Projekt**, und wie tagge ich sie dauerhaft? | Annotationsschicht `Tag`/`TAGGED` in `mailarc-core`; Cluster (Topic, Community) werden per Klick zu Tags befördert; Vorschläge nach jedem Rebuild (§4, §5.7) |
| B2 | Welche Mails sind **wichtig**, worum geht es? | Deterministischer, erklärbarer Score `Message.importance` + `importance_reasons`; `Topic.keywords` per TF-IDF (§5.5, §5.6) |
| B3 | Welche **versteckten Beziehungen** findet FalkorDB? | `algo.labelPropagation` (Communities), `algo.pageRank` (Antwort-Zentralität), `algo.SPpaths`/`algo.BFS` (Explorer), gewichteter PageRank für Adressen (§5.2–5.4) |
| B4 | Wie werden Beziehungen und Analyse-Ergebnisse **als Graph** gezeigt? | Seite `/graph` mit cytoscape.js als erster gewrappter React-Komponente; Teilgraphen je Topic/Mail/Adresse/Tag/Community; Knotengröße nach wählbarem Gewicht (§6, §7) |

### 1.1 Getroffene Entscheidungen (mit dem Nutzer abgestimmt)

1. **Tags: Vorschlag + Bestätigung.** Die Analyse liefert Cluster als Vorschlag, der Nutzer benennt und befördert. Nach jedem Rebuild werden weitere Mails je Tag vorgeschlagen (gemeinsamer Thread / Topic / Community), Übernahme per Klick oder `tag_auto_accept` in der Config. Kein vollautomatisches Taggen: `Topic.id` ist ein Hash der Mitglieder und bei jedem Rebuild neu (`derived/model.py:120–143`).
2. **Graph-Bibliothek: cytoscape.js über react-cytoscapejs** (beide MIT; npm: `cytoscape 3.34.2`, `react-cytoscapejs 2.0.0`, peer `react >=15`, `cytoscape ^3.2.19`; optional `cytoscape-fcose 2.2.0`). Lokales JSX-Asset nach dem Muster `appkit_mantine/maps_base.py` + `tiptap.py`.
3. **Wichtigkeit als Properties auf `Message`** (`importance`, `importance_reasons`, `importance_version`) — Präzedenz `Message.embedding`/`embedding_model`: `WRITE_EMBEDDINGS` ist das eine Katalog-Statement, das Ground Truth schreibt, `CLEAR_EMBEDDINGS` setzt per `SET … = null` zurück (`queries/statements/embedding.py:170–181, 279–287`). Kein DELETE, die Lösch-Guards in `rebuild.py` bleiben unberührt.

### 1.2 Regeln, die weiter gelten

- Ground Truth wird nie aus Abgeleitetem; jede abgeleitete Kante trägt `method` und `score` (Spec §5.2).
- Ein LLM liest nur zur Abfragezeit über MCP, schreibt nie (Spec §3.2). Keywords und Scores sind deterministisch.
- Hexagonale Schichtung (CLAUDE.md §6): Tag-Speicher in `mailarc-core` (Annotation an Ground Truth, wie `Address.remote_trusted`), Algorithmen/Vorschläge in `mailarc-analytics`, Seite in `mailarc-ui`, Verdrahtung nur in `app/`.
- Tests zuerst; Graph-Tests mit Marker `graph_local` gegen den session-scoped vendored FalkorDB; UI-States über `FakeGraph` + Registry-Snapshot.
- Dateien ≤ 1000 Zeilen: `rebuild.py` (304) bleibt Orchestrator, jede neue Stufe ein eigenes Modul.
- Nie `.state/` anfassen; Browser-Prüfung über `task agent:app` (8081), `task agent:derive`.

---

## 2. Repo-Fakten, die diese Spec trägt (verifiziert)

- **Graph:** ein Graph je Installation (`GraphConfig.graph_name = "mail-archive"`). FalkorDB **4.20.3** vendored (`scripts/vendor_falkordb.py:45`), Client `falkordb 1.7.0`, `runic-py 0.6.0`, Reflex **0.9.10** (Vite/react-router, React 19.2), `appkit-mantine 1.14.0`.
- **Prozeduren im vendored Binary** (`strings falkordb.so`): `algo.pageRank`, `algo.WCC`, `algo.labelPropagation`, `algo.betweenness`, `algo.HarmonicCentrality`, `algo.SPpaths`, `algo.SSpaths`, `algo.BFS`, `algo.MSF`, `dbms.procedures`. Config-Keys: `nodeLabels`, `relationshipTypes`, `maxIterations` (LPA), `samplingSize`/`samplingSeed` (betweenness), `sourceNode`/`targetNode`/`relTypes`/`maxLen`/`relDirection`/`pathCount` (SPpaths). **Falle:** die Prozeduren werfen bei unbekanntem Label/Typ (leerer Graph vor dem ersten `CO_ADDRESSED`-Merge) → Probe + Guard je Aufruf (§5.1).
- **runic 0.6 kann kein Statement mit `CALL` beginnen:** `select()` emittiert immer `MATCH (n:Label)`, `.call()` ist eine Mid-Pipeline-Klausel (verifiziert: `MATCH (m:Message) CALL algo.pageRank(...)` würde je Zeile laufen). Alle `algo.*`-/`dbms.*`-Statements sind daher **Roh-Cypher-Strings im Katalog**, Präzedenz `VECTOR_INDEX_OPTIONS` (`queries/statements/search.py:402`); `rows_of` schickt `str` durch `session.execute` (`queries/rows.py:142–145`), `parameters_of` liest `$name` aus Strings, der Katalog-AST-Test akzeptiert `ast.Constant`. Immer `node.id AS id` projizieren, nie ein Node-Objekt zurückgeben.
- **`CO_ADDRESSED` ist gerichtet gespeichert, kleinere id zuerst** (`derived/model.py:560–590`). Ein PageRank darüber wäre ein Artefakt der id-Reihenfolge → Adress-Zentralität als gewichteter, ungerichteter PageRank in Python über `CorrespondentFindings.pairs` (liegt im Rebuild ohnehin im Speicher). FalkorDB-PageRank nur, wo die Kante echt gerichtet ist: `REPLIES_TO`.
- **Konversationen brauchen kein `algo.WCC`:** `build_topics` kennt kein `REPLIES_TO`-Signal; `DisjointSet` (`derived/partition.py`) über `(id, parent_id)` liefert account-übergreifende Konversationen in O(N), deterministisch → siebtes exaktes Signal `TopicSignal.CONVERSATION = 0.9`.
- **Rebuild-Guards:** `_DERIVED_NODE_DELETE` pinnt `Group|Topic|Template`, `_DERIVED_EDGE_DELETE` pinnt `CO_ADDRESSED` per exaktem Regex zur Importzeit (`derived/rebuild.py:79–160`). Jedes neue abgeleitete Label/jede Kante muss dort und in `_NODE_DELETIONS`/`_EDGE_DELETIONS` eingetragen werden; `Tag` darf von keinem Regex erreichbar sein.
- **Jobs:** `SyncJobKind` (`mailarc_core/database/entities.py:63`) bleibt bei vier Kinds; `DERIVE` trägt alle neuen Stufen. `app/worker.py::_STAGES = tuple(RebuildStage)` passt sich an; `tests/test_worker_analysis.py:160–182` pinnt `total=5` → 10.
- **`MessageFacts.addressed` mischt To und Cc** (`derived/reader.py:184–206`), kennt keine Reply-Zahl, keine Labels → eigener gepagter Read `MESSAGE_SIGNALS`, nicht `MESSAGE_RELATIONS` erweitern (dessen fünf optionale Expansionen kreuzmultiplizieren).
- **Reflex-Query-Parameter:** `RouterData.page` ist deprecated; lesen über `self.router.url.query_parameters` (`reflex/istate/data.py:128–160`). Navigation über `rx.redirect(f"{routes.GRAPH}?view=topic&id=…")` aus einer `pill_action`.
- **Provider-Flags:** Nur Gmail bringt `IMPORTANT`/`STARRED` als `Label`-Namen in den Graph; IMAP `\Flagged` und M365-Flags werden nicht importiert. Der Grund „flagged by the provider“ ist damit heute Gmail-only und sagt das auch.
- **Auto-Accept schreibt nie `rebuild_derived`:** `TAGGED` gehört core; der Katalog-Test `test_it_only_ever_merges_a_derived_label` pinnt, dass analytics nur abgeleitete Labels merged. Auto-Accept läuft in `app/derive.py` nach dem Rebuild über `TagRepository` — `app` darf beide Schichten nennen.

---

## 3. Zielarchitektur

### 3.1 Drei Schichten im Graph

| Schicht | Paket | Besitzt | Überlebt Rebuild | Überlebt Account-Clear-out |
| --- | --- | --- | --- | --- |
| Ground Truth | `mailarc_core.archive` | Message, Address, Thread, Label, Attachment, Account + Kanten | ja | Messages exklusiv des Accounts werden `DETACH DELETE`d (`archive/purge.py`) |
| **Annotation (neu)** | `mailarc_core.archive` (`tags.py`) | `Tag`, `TAGGED`, `Address.remote_trusted` (bestehend) | **ja** — kein Delete-Regex erreicht sie | `Tag` bleibt; `TAGGED` verschwindet mit der Message. Verwaiste Tags zeigen Zähler 0 und sind löschbar; `AccountEraser` bleibt unverändert |
| Abgeleitet | `mailarc_analytics.derived` | Group, Topic, Template, **Community**; CO_ADDRESSED, ADDRESSED_GROUP, ABOUT, INSTANCE_OF, **MEMBER_OF, IN_CIRCLE, SUGGESTED**; **`Message.importance*`, `Address.rank*`, `Topic.keywords`** | nein — gelöscht/genullt und neu berechnet | mit ihren Endpunkten |

### 3.2 Neue Knoten, Kanten, Properties

| Element | Deklaration | Schlüssel / Properties | Schreibt | Löscht |
| --- | --- | --- | --- | --- |
| `Tag` | `mailarc_core/archive/model.py` (nach `Message`; Klassenreihenfolge ist tragend) | `id = "tag:<slug>"` (PK, UNIQUE), `name`, `color: str\|None`, `origin: TagOrigin(manual\|topic\|community)`, `created_at` | `TagRepository.create` | nur `TagRepository.delete` |
| `TAGGED` Message→Tag | core `Tagged(Edge)`; `Tag.messages` (INCOMING, `edge_model=Tagged`); `Message.tags: Any = Relation("TAGGED", OUTGOING, target="Tag")` (gleiche `Any`-Falle wie `replies_to`) | `source: TagSource(manual\|accepted\|auto)`, `at` | `TagRepository.tag_messages` | `untag`, Message-Purge |
| `Tag.suggested` | core, INCOMING `SUGGESTED` ohne edge_model (Präzedenz `Address.co_addressed`) | — | — | — |
| `Community` | `mailarc_analytics/derived/model.py` | `id = "community:<sha256[:32] sortierter Adress-ids>"`, `size`, `message_count`, `label` (häufigste Domain, Tie → bestplatzierte Adresse), `method="lpa"`, `first_seen`, `last_seen`; Relationen `members` (INCOMING `MEMBER_OF`, Edge `MemberOf`), `messages` (INCOMING `IN_CIRCLE`, Edge `InCircle`) | `communities.write_communities` | `DELETE_COMMUNITIES` |
| `MEMBER_OF` Address→Community | analytics `MemberOf(Edge)` | `rank` (Adress-Zentralität) | ebd. | mit Knoten |
| `IN_CIRCLE` Message→Community | analytics `InCircle(Edge)` | `score` = Anteil der Teilnehmer in der Community, `method="participants"` | ebd. | mit Knoten |
| `SUGGESTED` Message→Tag | analytics `Suggested(Edge)` | `score`, `method: thread\|topic\|community` | `suggestions.write_suggestions` | `DELETE_SUGGESTED` (Kanten-Delete, eigener Regex) |
| `Message.importance: float`, `importance_reasons: list[str]`, `importance_version: str` | core model | — | `WRITE_IMPORTANCE` | `CLEAR_IMPORTANCE` (`SET … = null`) |
| `Address.rank: float`, `rank_version: str` | core model | — | `WRITE_ADDRESS_RANKS` | `CLEAR_ADDRESS_RANKS` |
| `Topic.keywords: list[str]` | analytics model | — | `MERGE_TOPIC_KEYWORDS` (`MATCH` + `SET`) | mit Knoten |

### 3.3 Rebuild-Stufen (`RebuildStage`, in dieser Reihenfolge)

`DELETE → READ → CORRESPONDENTS → CENTRALITY → COMMUNITIES → TOPICS → KEYWORDS → TEMPLATES → IMPORTANCE → SUGGESTIONS`

- `DELETE` leert zusätzlich `DELETE_COMMUNITIES`, `DELETE_SUGGESTED` und führt beide `CLEAR_*` aus.
- `READ` bekommt zwei gepagte Reads (`MESSAGE_REPLIES`, `MESSAGE_SIGNALS`); Konversationen werden hier per Union-Find gebildet und gehen als Signal 7 in `TOPICS`.
- `CENTRALITY` vor `COMMUNITIES` (Labels und `MEMBER_OF.rank` brauchen Ränge); `IMPORTANCE` nach `TEMPLATES` (`automation_score`) und `CENTRALITY` (Absender-Rang, Reply-PageRank); `SUGGESTIONS` zuletzt (braucht Topics, Communities, aktuelle `TAGGED`-Mitgliedschaft).

---

## 4. Phase 1 — Annotationsschicht (Tags) in `mailarc-core`

**Neu**

- `components/mailarc-core/src/mailarc_core/archive/tags.py` — `TagRepository(session)` (ein Statement je Verb, Builder-Konstanten mit Import-Zeit-Shape-Guards wie `archive/purge.py:198–270`) und `TagStore(graph_session)` als Fassade (Session je Aufruf wie `ArchiveReader`, `reader.py:60–78`).
  - `create(name, *, origin, color=None) -> TagSummary` (`session.add(Tag(...))`, Slug via `tag_id(name)`, Duplikat → `TagExists`)
  - `rename(tag_id, name)`, `recolor(tag_id, color)` — `session.get` + Attribut, dirty-tracked (Präzedenz `AddressRepository.trust_remote`, `repository.py:258–270`)
  - `delete(tag_id) -> bool` — `MATCH (n:Tag) WHERE n.id = $id DETACH DELETE n RETURN count(n) AS removed`, Regex `_TAG_DELETE` (nur Label `Tag`)
  - `tag_messages(tag_id, ids, *, source, at=None) -> int` — `unwind($rows).match(Message).match(Tag).merge_edge("m", Tagged, "t").set(source, at)`; der Store liest vorher die Mitgliedschaft und sendet nur neue Zeilen, damit `at`/`source` einer früheren Entscheidung bleiben
  - `untag(tag_id, ids) -> int` — `DELETE r` auf der Kantenvariable, Guard `_UNTAG_SHAPE` (nie `DETACH`)
  - `tags_of(ids) -> dict[str, tuple[TagSummary, ...]]`, `members(tag_id, *, limit, offset)`, `list_tags() -> tuple[TagSummary, ...]` (Zähler per traverse + `count()`)
  - `TagStore.promote(name, member_ids, *, origin) -> TagSummary` (create + tag in einer Session)
- `graph_migrations/versions/<rev>_annotation_layer.py` — `op.create_constraint("UNIQUE", "NODE", "Tag", ["id"])` (Constraint legt eigenen Index an, Baseline-Kommentar `fc2f7a8d4b66:44–50`), `op.create_range_index("Message", "importance")`, `op.create_range_index("Address", "rank")` (Phase 2 braucht sie; eine Migration hält die Historie kurz).
- Tests: `components/mailarc-core/tests/archive/test_archive_tags.py` (rein: Slug, `TagSummary`, Guards verweigern ein `DETACH` auf `Message`), `test_archive_tags_local.py` (`graph_local`, Fixtures aus `tests/archive/conftest.py:7–32`): create/rename/delete-Roundtrip; zweimal taggen behält erstes `at`; untag lässt die Message; Purge des Accounts lässt den Tag mit Zähler 0; UNIQUE weist zweiten Tag mit gleicher id ab. `tests/test_graph_migrations_annotation_local.py` nach Vorbild `test_graph_migrations_vector_local.py`.

**Ändern**

- `archive/model.py` — `TagOrigin`, `TagSource` (StrEnum), `TagSummary` (frozen pydantic: id, name, color, origin, created_at, message_count), `Tagged(Edge)`, `Tag(Node)`, `Message.tags`; die Phase-2-Properties `importance`, `importance_reasons`, `importance_version` auf `Message` und `rank`, `rank_version` auf `Address` schon jetzt deklarieren (Migration und Writer müssen übereinstimmen).
- `archive/__init__.py`, `mailarc_core/__init__.py` — Exporte `Tag`, `Tagged`, `TagOrigin`, `TagSource`, `TagSummary`, `TagRepository`, `TagStore`, `TagExists`.
- `app/composition.py` — `@lru_cache tag_store()` + idempotentes `publish_tag_store()` (Kopie von `publish_archive_reader`, `:550–563`); `app/app.py` ruft es. Test in `tests/test_composition.py` (Präzedenz `test_the_ui_finds_the_reader_without_importing_the_app`, `:287`).
- `components/mailarc-analytics/tests/queries/test_queries_catalog.py` — `"Tag"` in `GROUND_TRUTH_LABELS` (analytics darf matchen, nie mergen).
- Docs: `docs/developer/data-model.md` neuer Abschnitt „The graph — annotations“ zwischen Ground Truth und Derived; `docs/user/importing-mail.md` „What is not deleted“ nennt Tags.

**Verifikation:** `uv run pytest components/mailarc-core -m graph_local`, `task agent:graph:upgrade`.

---

## 5. Phase 2 — Algorithmen, Wichtigkeit, Keywords, Vorschläge in `mailarc-analytics`

### 5.1 Fähigkeits-Probe und Guard

`derived/algorithms.py::graph_algorithms(session) -> frozenset[str]` führt `catalog.PROCEDURES` (`CALL dbms.procedures() YIELD name RETURN name`) aus, lower-cased, je Session gecacht (Präzedenz `semantic/search.py::has_fulltext_index`, `:174–185`). Jeder `algo.*`-Aufruf zusätzlich: `ResponseError` (unbekanntes Label/Typ auf leerem Graph) → Warnung + `DerivedCounts.algorithms_skipped += 1`, Stufe meldet 0, Rebuild läuft weiter. Prozedurnamen aus der Probe übernehmen (Binary schreibt `algo.WCC`/`algo.BFS`, Fehlermeldungen `algo.wcc`).

### 5.2 Neue Module (eine Stufe je Modul)

- `derived/algorithms.py` — `graph_algorithms`, `label_propagation(session, *, max_iterations) -> dict[address_id, int]`, `message_pagerank(session) -> dict[message_id, float]`, `address_betweenness(session, *, sampling_size, seed)` (optional, Probe-gated); jede liefert `{}` mit Warnung, wenn die Prozedur fehlt oder ablehnt.
- `derived/centrality.py` — `weighted_pagerank(pairs, *, damping, iterations) -> dict[str, float]`: Power-Iteration in Python über den ungerichteten, count-gewichteten Graphen, sortierte Iterationsreihenfolge (deterministisch); Kappe `centrality_max_edges`; `write_address_ranks(session, ranks, version)`.
- `derived/conversations.py` — `conversation_edges(facts, replies: Mapping[id, parent]) -> tuple[SimilarityEdge, ...]` via `DisjointSet` über `thread_id` und Reply-Parent; n-1 Kettenkanten je Komponente, `method="conversation"`, Gewicht `SIGNAL_WEIGHTS[TopicSignal.CONVERSATION]`.
- `derived/communities.py` — `build_communities(facts, membership, ranks, config) -> CommunityFindings` (< `community_min_size` fällt weg; `community_id()` Digest wie `topic_id()`; Label; `IN_CIRCLE` zur Community mit ≥ `circle_min_share` der Teilnehmer einer Message, größter Anteil gewinnt, Tie → kleinere id); `write_communities(session, findings)`.
- `derived/importance.py` — `score_messages(facts, signals, *, sender_rank, reply_rank, template_scores, own, config) -> tuple[ImportanceScore, ...]`: gewichtete Summe, auf 0..1 geklemmt, Gründe aus festem Vokabular: `"replied by you"`, `"3 replies"`, `"sent by a central correspondent"`, `"addressed directly"` (Account-Adresse in `SENT_TO`, nicht `COPIED_TO`), `"few recipients"`, `"has attachments"`, `"looks automated"` (negativ, `INSTANCE_OF` mit hohem `automation_score`), `"flagged by the provider"` (Label-Namen `IMPORTANT`/`STARRED`, Gmail reicht sie unverändert durch: `mailarc_google/source/mapping.py:174–186`). `IMPORTANCE_VERSION = "1"`; `write_importance(session, scores)`.
- `derived/keywords.py` — `topic_keywords(clusters, texts, config) -> dict[topic_id, tuple[str, ...]]`: Tokenisierung (lowercase, ≥ 3 Buchstaben, kleine DE/EN-Stoppliste, Refs/Zahlen raus), TF je Topic, IDF über *Topics* (`log(T / (1 + df))`), Top `topic_keyword_count`. Kostendeckel `topic_keyword_members` × `topic_keyword_chars` (Kürzung per `left()` in Cypher, Präzedenz `MESSAGES_NEEDING_EMBEDDING`, `statements/embedding.py:140`). `write_keywords(session, found)`.
- `derived/suggestions.py` — `suggest(tagged: Mapping[tag_id, frozenset[str]], groups: Sequence[Grouping]) -> tuple[Suggestion, ...]`: ein `Grouping(kind: thread|topic|community, members)` mit `k ≥ tag_suggest_min_tagged` getaggten Mitgliedern und `k/n ≥ tag_suggest_min_share` schlägt jedes ungetaggte Mitglied vor, `score = weight[kind] · k/n` (Maximum über Gruppen, Methode des Maximums); `write_suggestions(session, found)`.

### 5.3 Katalog-Statements

- `queries/statements/algorithms.py` (Roh-Strings): `PROCEDURES`, `LABEL_PROPAGATION` (`$max_iterations`), `MESSAGE_PAGERANK`, `ADDRESS_BETWEENNESS` (`$sampling_size`, `$sampling_seed`), `SHORTEST_PATHS` (`$left`, `$right`, `$max_len`, `$path_count`; liefert `ids`, `types`), `NEIGHBOURHOOD` (`algo.BFS` ab `$id`, `$depth`; für den Explorer).
- `queries/statements/reads.py` (Builder): `MESSAGE_REPLIES` (gepagt: `id`, `parent`), `MESSAGE_SIGNALS` (gepagt: `id`, `to` collect SENT_TO, `reply_count`, `replied_by` collect Reply-Absender, `label_names`, `has_attachments`), `MESSAGE_TEXTS` (`$ids`: `id`, `subject`, `left(body_clean, $max_chars) AS body`), `TAGGED_MEMBERSHIP` (`tag_id`, `message_id`).
- `queries/statements/writes.py`: `DELETE_COMMUNITIES`, `DELETE_SUGGESTED` (Kanten-Delete, gewurzelt bei `Tag`: `select(alias(Tag,"t")).traverse(Tag.suggested, to="m", edge=alias(Suggested,"r")).with_(r, limit=$batch).delete(r).returning(count("r").as_("removed"))`), `MERGE_COMMUNITIES`, `MERGE_MEMBER_OF`, `MERGE_IN_CIRCLE`, `MERGE_SUGGESTED`, `MERGE_TOPIC_KEYWORDS`, `WRITE_IMPORTANCE`, `CLEAR_IMPORTANCE`, `WRITE_ADDRESS_RANKS`, `CLEAR_ADDRESS_RANKS` (alle vier nach `WRITE_EMBEDDINGS`/`CLEAR_EMBEDDINGS`).
- `queries/statements/analysis.py`: `COUNT_COMMUNITIES`, `TOP_COMMUNITIES` (`$limit`), `TOP_IMPORTANT` (`$limit`; id, subject, sent_at, sender, importance, reasons), `TOPIC_KEYWORDS` (`$limit`), `SUGGESTION_COUNTS` (je Tag), `TAG_SUGGESTIONS` (`$tag`, `$limit`).
- `queries/catalog.py` — jede neue Konstante in `CATALOG` und `__all__` (Test `test_the_catalogue_lists_every_statement_in_the_module` pinnt das); Docstring „one exception remains“ → „die Roh-Statements sind der Index-Read und die Prozeduraufrufe“.

### 5.4 Änderungen an Bestehendem

- `derived/model.py` — `TopicSignal.CONVERSATION`, `SIGNAL_WEIGHTS[...] = 0.9`, `RebuildStage` (10 Stufen), Value Objects `MessageSignals`, `ImportanceScore`, `CommunityFacts`, `CommunityFindings`, `Suggestion`, `Grouping`; Klassen `MemberOf`, `InCircle`, `Suggested`, `Community`; `Topic.keywords`; `DerivedCounts` + `communities`, `circles`, `ranked_addresses`, `ranked_messages`, `keyworded_topics`, `scored_messages`, `suggestions`, `algorithms_skipped` (distinkte Werte in `tests/test_derive.py::FOUND`).
- `derived/config.py` — `community_min_size=3`, `community_max_iterations=20`, `circle_min_share=0.5`, `centrality_max_edges=2_000_000`, `betweenness_sampling=0` (0 = überspringen), `topic_keyword_count=8`, `topic_keyword_members=20`, `topic_keyword_chars=2000`, `tag_suggest_min_tagged=2`, `tag_suggest_min_share=0.3`, `tag_auto_accept=False`, `tag_auto_accept_min_score=0.6`.
- `derived/reader.py` — `read_replies`, `read_signals`, `read_texts(session, ids, max_chars)`, `read_tagged`.
- `derived/rebuild.py` — Regex-Label-Gruppe → `Group|Topic|Template|Community`; neuer `_SUGGESTED_EDGE_DELETE`; `_NODE_DELETIONS += DELETE_COMMUNITIES`; `_EDGE_DELETIONS += DELETE_SUGGESTED`; `_clear_properties(session)` für beide `CLEAR_*`; Stufenverdrahtung nach §3.3; `build_topics(..., extra_edges=(*conversations, *extra_edges))`.
- `queries/reports.py` — `AnalyticsReader.communities(limit)`, `important_messages(limit)`, `topic_keywords(limit)`, `suggestion_counts()`, `suggestions_for(tag_id, limit)`; Rows in `queries/model.py` (`CommunityRow`, `ImportantMessageRow`, `TagSuggestionRow`, `TopicKeywordsRow`).
- `app/derive.py` — nach `rebuild_derived`: `_auto_accept(session, counts)` wenn `analytics_config().tag_auto_accept`: liest `suggestions_for` ≥ Schwelle, ruft `TagRepository(session).tag_messages(..., source=TagSource.AUTO)`; Log je Tag. `tests/test_derive.py::PHRASES` erweitern.
- `tests/test_worker_analysis.py` totals 5 → 10.

### 5.5 Tests

- Rein: `tests/derived/test_derived_centrality.py` (Hub rankt höchstens; symmetrisch → symmetrisch; deterministisch), `test_derived_conversations.py` (Thread + Reply über zwei Accounts; Einzelgänger bleibt allein), `test_derived_communities.py` (Digest stabil unter Reihenfolge; Label = Domain; `IN_CIRCLE`-Schwelle; unter Minimum fällt weg), `test_derived_importance.py` (jeder Grund feuert allein; Vorlage zieht runter; geklemmt; Gründe sortiert; Version gestempelt), `test_derived_keywords.py` (Stoppwörter/Refs raus; diskriminierender Term gewinnt; Deckel), `test_derived_suggestions.py` (min tagged, min share, beste Gruppe, bereits getaggt ausgeschlossen, Auto-Accept-Schwelle), `test_derived_rebuild.py` (vier Node-Deletes, zwei Edge-Deletes, ein `Tag`-Delete verweigert den Import, `SUGGESTED`-Delete muss `DELETE r` sein), `test_derived_model.py` (Counts, Stufenreihenfolge).
- `graph_local`: `tests/derived/test_derived_algorithms_local.py` (Probe listet `algo.labelpropagation`; LPA trennt im gepflanzten Korpus `kunde.example` von `nordlicht.example`; auf leerem Graph wird jeder Aufruf übersprungen und gezählt), `test_derived_rebuild_local.py` erweitern (Counts; byte-identischer zweiter Lauf, siehe R1; Ground Truth unberührt außer den vier genullten Properties; ein vor dem Rebuild gepflanzter `Tag` überlebt mit Kanten), `tests/queries/test_queries_catalog_local.py` (`SCALARS` + `max_iterations`, `sampling_size`, `sampling_seed`, `left`, `right`, `max_len`, `path_count`, `tag`, `version`, `max_chars`, `depth`; `ROWS` + neue Merges).
- `tests/test_derive.py` — `tag_auto_accept` aus → kein core-Write; an → `tag_messages(source=auto)` nur für Scores ≥ Schwelle.

**Verifikation:** `task test`, dann `task agent:derive` und `task agent:exec -- uv run python -c '…Community zählen…'`.

---

## 6. Phase 3 — Teilgraph-Lese-API in `mailarc-analytics`

**Neu**

- `queries/statements/graph.py` (Builder, klein gehalten — der Reader komponiert statt zu kreuzmultiplizieren): `MESSAGE_ADDRESSES` (`$id`: Adress-id, Kantentyp), `THREAD_SIBLINGS` (`$id`, `$limit`), `REPLY_CHAIN` (`$id`; `REPLIES_TO` beide Richtungen, festes `*1..3` — Variable-Length-Grenzen sind keine Parameter; Roh-String, falls der Builder kein var-length kann), `MESSAGE_TOPICS`, `MESSAGE_TAGS`, `MESSAGE_CIRCLE`, `TOPIC_MEMBERS` (`$id`, `$limit`, sortiert `importance` desc, id asc), `TOPIC_PARTICIPANTS`, `ADDRESS_NEIGHBOURS` (`$id`, `$limit`; beide Pfeile, `count`), `ADDRESS_MESSAGES`, `TAG_MEMBERS`, `COMMUNITY_MEMBERS`, `COMMUNITY_MESSAGES`, `OVERVIEW_TOPICS`, `OVERVIEW_COMMUNITIES`, `OVERVIEW_TAGS`, `OVERVIEW_TOPIC_CIRCLE` (`(t:Topic)<-[:ABOUT]-(m)-[:IN_CIRCLE]->(c)` count), `OVERVIEW_TAG_TOPIC`; dazu `SHORTEST_PATHS`/`NEIGHBOURHOOD` aus Phase 2.
- `queries/graphs.py` — `GraphReader(graph_session)`: `topic(id, *, limit)`, `message(id, *, depth, limit)`, `address(id, *, limit)`, `tag(id, *, limit)`, `community(id, *, limit)`, `overview(*, limit)`, `path(left, right, *, max_len)`, `expand(id, kind, *, limit)` (ein Hop, vom Aufrufer gemerged). Jede Methode liefert `Subgraph`; Grad in Python über die gelieferte Kantenmenge, `pagerank`/`importance`/`count` aus Properties, je Teilgraph auf 0..1 normalisiert; `truncated` je Sicht.
- `queries/model.py` — `NodeKind(StrEnum)`, `GraphNode(id, kind, label, weights: dict[str, float], props: dict[str, str])`, `GraphEdge(source, target, kind, weight, label)`, `Subgraph(nodes, edges, truncated, notice)` — frozen.
- Tests: `tests/queries/test_queries_graphs.py` (Fake-Session nach Statement, Muster `mailarc-ui/tests/insights_archive.py:58–146`: Dedup, Grad, Normalisierung, Truncation, Expand-Merge), `test_queries_graphs_local.py` (`graph_local`: Topic-Sicht des gepflanzten `NORD-42`-Projekts hält p1–p5 und Anna/Thomas; Ego von p2 enthält p1 über Reply und Thread; Pfad Anna→Thomas ist ein Hop; Overview verbindet Topic und Circle).

**Ändern:** Exporte in `queries/__init__.py`, `mailarc_analytics/__init__.py`; `app/composition.py` `graph_reader()` + `publish_graph_reader()`; `app/app.py`.

---

## 7. Phase 4 — Seite `/graph` und Kit-Wrapper in `mailarc-ui`

**Neu**

- `kit/graph_canvas.jsx` — Default-Export: `cytoscape`-Instanz in `useRef`, `useEffect` auf `elements`/`stylesheet`/`layout`; `cy.on('tap','node')` → `onSelect(id)`, `dbltap` → `onExpand(id)`, Tap auf Hintergrund → `onBackground()`; `fitToken`-Wechsel → `cy.fit()`; `selected` → `cy.$id(selected).select()`; Stylesheet nutzt `mapData(weight, 0, 1, 14, 52)` für width/height und `data(color)` je Knoten. Props im JSX camelCase lesen (`fit_token` → `fitToken`).
- `kit/graph.py` — `_GRAPH_JSX = rx.asset("graph_canvas.jsx", shared=True)`; `class _GraphCanvas(NoSSRComponent)`: `library = _GRAPH_JSX.importable_path`, `tag = "GraphCanvas"`, `is_default = True`, `lib_dependencies = ["cytoscape@3.34.2", "react-cytoscapejs@2.0.0"]`, Props `elements: rx.Var[list[dict[str, Any]]]`, `stylesheet`, `layout: rx.Var[dict[str, Any]]`, `selected: rx.Var[str]`, `fit_token: rx.Var[int]`, Events `on_select: rx.EventHandler[lambda id: [id]]`, `on_expand`, `on_background: rx.EventHandler[lambda: []]`, `add_imports` wie `appkit_mantine/tiptap.py:250–262`; öffentlicher Builder `graph_canvas(**props)` (ein `rx.box` mit `.ma-graph-canvas` und fester Höhe). Klasse bleibt privat; `kit/__init__.py` exportiert nur `graph_canvas`.
- `assets/css/mail-archive.css` — `.ma-graph-canvas` (Höhe, Hairline, Grund); Knotenfarben je Art als `--ma-graph-*` *und* dieselben Hexwerte in `mailarc_ui/theme.py::Palette` (cytoscape liest keine CSS-Variablen; Python reicht konkrete Hexe ins Stylesheet).
- `graph/model.py` — `GraphView`, `SizeBy` (uniform / degree / pagerank / importance / count), `LayoutName` (cose / concentric / breadthfirst; fcose optional), `NodeCard` (frozen: id, kind, title, lines), `elements_of(subgraph, *, size_by, hidden_kinds) -> list[dict]`, `stylesheet_of(palette)`, `layout_of(name)` — rein, ohne Reflex testbar.
- `graph/reads.py` — `graph_reader()`, `tag_store()`, `analytics_reader()` Registry-Lookups (Muster `dashboard/reads.py:47–68`); `picker_options(view)` aus `AnalyticsReader.topics/communities`, `TagStore.list_tags`, `ArchiveReader.list_messages`.
- `tags/{model,state,components}.py` — `TagActionsState(rx.State, mixin=True)`: Vars `tags: list[TagView]`, `promote_name`, `promote_errors: FieldErrors`; Handler `refresh_tags`, `tag_message`, `untag_message`, `accept_suggestion`, `accept_all(tag_id)`, `promote(kind, id)` (Name über kit `input_field` + `FieldErrors`), `delete_tag`, `rename_tag`. Komponenten `tag_chips(state, message_id)`, `promote_form(state)`, `suggestion_rows(state)`. Geteilt von Insights und Explorer (Präzedenz `MessageDetailState`).
- `graph/state.py` — `GraphExplorerState(TagActionsState, MessageDetailState, rx.State)`: Vars `view`, `picked_id`, `depth`, `size_by`, `layout_name`, `hidden_kinds: list[str]`, `subgraph: SubgraphView` (pydantic), `elements`/`stylesheet`: `list[dict]` (dokumentierte Ausnahme wie `dashboard/state.py:156–163`), `selected_node: NodeCard`, `fit_token`, `loading`, `error`, `truncated_notice`; Handler `load` (liest `self.router.url.query_parameters` `view`/`id`), `choose_view`, `pick`, `set_depth`, `set_size_by`, `set_layout`, `toggle_kind`, `select_node(id)` (Message → `_open_message(id, eml_sha256)` via `archive_reader().messages_by_ids`), `expand_node(id)` (merged `GraphReader.expand`), `clear_selection`, `fit`, `show_path(other_id)`; Hintergrund-Reads nach `_answered` (`insights/state.py:106–118`).
- `graph/components.py` — `explorer_panel()`: links `column_card` (segmentierter View-Picker, durchsuchbares `select_field`, `number_field` Tiefe, `select_field` Größe-nach, Art-Toggles als `range_switch`, `select_field` Layout, `soft_button("Fit")`), Mitte `column_card(graph_canvas(...))`, rechts `column_card` Details: `rx.match(kind, …)` → `message_tabs(GraphExplorerState)` + `tag_chips`; Topic/Community: Mitglieder-`scroll_table` + `promote_form`; Adresse; Tag mit Vorschlägen. Layout wie `search/components.py:218–245` (`mn.splitter`).
- `pages/graph.py` — `@public_page(route=routes.GRAPH, title="Graph", on_load=[GraphExplorerState.load])`.
- Tests: `tests/test_ui_graph_model.py` (elements/stylesheet/layout), `test_ui_graph_state.py` (Fake-`GraphReader` in der Registry per `snapshot/restore`; Query-Params treiben `load`; select öffnet Message; expand merged; Größe-nach gewichtet ohne Re-Read um), `test_ui_tags_state.py` (Fake-`TagStore`; promote validiert Namen; accept-all ruft `tag_messages(source=accepted)`), `test_ui_kit_components.py` (`graph_canvas` rendert `GraphCanvas` mit den Props), `test_ui_pages.py::PAGES` Zeile `PageSpec(name="graph", page=graph.graph_page, route=routes.GRAPH, title="Graph", primes=("GraphExplorerState.load",))`.

**Ändern:** `shell/routes.py` (`GRAPH = "/graph"`, `ALL_ROUTES`), `shell/navigation.py` (Menüpunkt, Icon `waypoints`), `tests/test_ui_shell_navigation.py:149–157` (Menü hat vier Seiten), `app/app.py` (Import `graph`; `publish_tag_store`, `publish_graph_reader`), `insights/components.py` Topic-Zeile bekommt `pill_action("Graph", on_click=rx.redirect(...))`, `search/components.py::_result_row` „Show in graph“, `tests/test_documented_routes.py` (Docs müssen `/graph` nennen), `tests/test_ui_forms_are_one_look.py` (Promote-Formular in FORMS eintragen).

**Verifikation:** `task test`; `task agent:app` auf 8081 → `/graph?view=topic&id=<Topic-id von /insights>`; Browser-Konsole: Chunk lädt `graph_canvas.jsx`, `cytoscape` steht in `.web/package.json`; Screenshot mit unterschiedlich großen Knoten bei `size_by=importance`.

---

## 8. Phase 5 — Insights, MCP, Dokumentation

- `insights/state.py` hostet `TagActionsState`; `_read_everything` liest Communities, wichtige Mails, Topic-Keywords (via `_answered`), Tags mit Vorschlagszählern. `insights/components.py`: `communities_card`, `tags_card` (Chips, Vorschlagszähler, „Accept all“, „Delete“), `important_card` (`scroll_table`: Subject, Absender, `score_bar(importance)`, Gründe als `label_chip`), Keyword-Spalte in `topics_card`; jede Karte mit „Graph“-`pill_action`. `insights/model.py`: `CommunityView`, `ImportantMessageView`, `TagView`, `TopicView.keywords`. Tests in `test_ui_insights_state.py` mit `FakeGraph`-Zeilen für die neuen Statements.
- MCP (`components/mailarc-mcp/src/mailarc_mcp/server/{server,reads,model}.py`): vier read-only Tools `important_messages(limit)`, `topic_messages(topic_id, limit)` (Mitglieder nach Wichtigkeit, je Subject/Absender/Datum/Preview über `ArchiveReader.messages_by_ids` — der §3.2-konforme Weg, ein Thema zur Abfragezeit zusammenfassen zu lassen), `tags()`, `tagged_messages(tag, limit)`. Kein `subgraph`-Tool (Knoten/Kanten-Dump ist Rauschen für ein Modell). `ArchiveAccess` bekommt `tags: TagFactory`; `app/mcp_server.py::archive_access` reicht `tag_store`. Tests: „six tools“ → zehn; je Tool Antworttest mit Fake-Access.
- Docs: `docs/user/insights.md` (neu: Topics, Communities, Wichtigkeit, Tags/Vorschläge, Auto-Accept), `docs/user/graph-explorer.md` (neu), `docs/user/configuration.md` neuer Abschnitt `### Analytics — app.analytics / APP_ANALYTICS_` (jedes Feld, Tag-Flags zuerst), `docs/developer/data-model.md` (Annotationsschicht; neue abgeleitete Knoten/Kanten; die vier genullten Properties; Prozedur-Statements), `docs/developer/graph-explorer.md` (Kit-Wrapper, Asset-Pfad, Prop/Event-Vertrag), `docs/developer/mcp-server.md` (vier Tools), `docs/.vitepress/config.mts` Sidebar. `README.md` Feature-Liste. `docs/diagrams/graph-model.drawio` um Annotation + Community ergänzen.

---

## 9. Risiken und offene Punkte (mit Empfehlung)

- **R1 Determinismus von Label Propagation.** FalkorDBs LPA hat keinen Seed; zwei Läufe über einen unveränderten Graphen können bei mehrdeutigen Knoten abweichen und `test_every_node_id_edge_and_property_is_identical` (`test_derived_rebuild_local.py:611`) brechen. Empfehlung: `Community` per Mitglieder-Digest schlüsseln, `community_max_iterations` pinnen, Idempotenz auf **Partitionsebene** am gepflanzten Korpus prüfen (zwei eindeutige Cliquen). Flackert der byte-identische Test, `Community`/`MEMBER_OF`/`IN_CIRCLE` aus `_snapshot` ausnehmen. Fallback: ~50-zeilige deterministische Python-LPA über `found.pairs` (sortiert, kleinstes Label bei Tie) hinter derselben `build_communities`-Signatur.
- **R2 Richtung von `CO_ADDRESSED` in LPA/Betweenness.** Im ersten `graph_local`-Test prüfen (ein Hub darf nicht von der id-Reihenfolge abhängen). Adress-PageRank ist deshalb Python; Betweenness bleibt optional (`betweenness_sampling=0`).
- **R3 Prozedurfehler auf frischem Graph** („unknown relationship-type“). Probe + Guard + `algorithms_skipped`; der Katalog-Sweep läuft nach den Merges, dort existieren die Typen.
- **R4 Performance ab 100k Mails.** LPA/PageRank in FalkorDB sind schnell; Python-PageRank über ≤ 2M Paare × 20 Iterationen ≈ 20–40 s → `centrality_max_edges` mit Zähler des Übersprungenen. Keywords sind durch `topics × members × chars` gedeckelt. `MESSAGE_SIGNALS` bleibt ein eigener gepagter Read. `CLEAR_*` sind ungebatchte `SET`s wie `CLEAR_EMBEDDINGS`.
- **R5 `react-cytoscapejs@2.0.0` unter React 19.** Peer-Range `react >=15.0.0` erfüllt (npm verifiziert). Fällt `bun install` trotzdem, `cytoscape` direkt aus dem JSX treiben — die Python-API bleibt gleich. Bundle ≈ 400 kB minified, nur `/graph` zahlt (NoSSR-Chunk).
- **R6 Reflex-Event-Spec für JSX-Callbacks.** `EventHandler[lambda id: [id]]` ist der tiptap-Präzedenzfall (`tiptap.py:58`); `on_background` mit `lambda: []`.
- **R7 `Topic.id` ist keine dauerhafte Referenz.** Ein `/graph?view=topic&id=…`-Link veraltet nach dem nächsten Rebuild; der State antwortet „this topic was recomputed — pick it again“. Tags sind die dauerhafte Referenz; ein beförderter Tag speichert `origin=topic`, nie die id.
- **R8 Vorschläge zwischen Rebuilds.** `SUGGESTED` wird je Rebuild neu berechnet; ein zwischendurch angelegter Tag hat bis zum nächsten Rebuild keine Vorschläge. Die Insights-Karte sagt das und bietet den Rebuild-Button an.
- **R9 Test-Pins, die absichtlich brechen.** `_NODE_DELETIONS`/`_EDGE_DELETIONS` (`test_derived_rebuild.py:240–248`), der Log-Test mit vierzehn Counts (`tests/test_derive.py:164–172`), `total=5` (`tests/test_worker_analysis.py:160–182`), Menü „drei Seiten“ (`test_ui_shell_navigation.py:149`), MCP „six tools“ — jeweils eine sichtbare, gewollte Änderung.
- **R10 `Message.importance` auf einem Ground-Truth-Knoten.** Gleiches Argument wie `embedding`: eine Property, die der Import nie schreibt, versioniert, genullt und neu berechnet. In `data-model.md` und im `Message`-Docstring benennen; Delete-Guards bleiben unberührt.
- **Offen — Provider-Flags.** IMAP `\Flagged`/M365-Flags importieren ist eine Provider-Änderung außerhalb dieses Umfangs; das Gründe-Vokabular bleibt ehrlich („flagged by the provider“ nur, wo ein Label es sagt).

---

## 10. Umsetzungsreihenfolge und Definition of Done

| Schritt | Inhalt | Gate |
| --- | --- | --- |
| 1 | Phase 1 Tags (core, Migration, Composition) | `graph_local`-Tests grün, `task agent:graph:upgrade` |
| 2 | Phase 2 Stufen 4–10, Katalog, Config, `app/derive.py` Auto-Accept | `task test`; `task agent:derive` zählt Communities/Suggestions |
| 3 | Phase 3 `GraphReader` + Statements | `test_queries_graphs*` grün |
| 4 | Phase 4 Kit-Wrapper, `/graph`, Tags-Mixin | Browser-Nachweis auf 8081 (Screenshot) |
| 5 | Phase 5 Insights-Karten, MCP-Tools, Docs | `tests/test_documented_routes.py`, `docs:build` |

Je Phase: `task format && task lint && task typecheck && task test` (Coverage ≥ 80 %), keine Datei > 1000 Zeilen, Conventional Commit je Phase (`feat(core): annotation layer …`, `feat(analytics): …`, `feat(ui): graph explorer …`), Memory aktualisieren (runic-`CALL`-Grenze, LPA-Determinismus, cytoscape-Wrapper-Muster).

**Nicht in diesem Umfang:** Personenauflösung über Adressen hinaus (Spec §13), Import von IMAP/M365-Flags, inkrementelle Neuberechnung (Phase 7 der Grund-Spec bleibt Voll-Rebuild), LLM-erzeugte Labels oder Zusammenfassungen im Graph.
