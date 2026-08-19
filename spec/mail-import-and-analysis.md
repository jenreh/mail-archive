# Spec: Mail-Import und Analyse in der GraphDB

> **Status:** freigegeben, nicht begonnen.
> **Umsetzbar ohne Vorwissen:** Diese Datei enthält alle Repo-Fakten,
> Bibliotheks-APIs und Entscheidungen, die zur Umsetzung nötig sind. Wo etwas
> noch zu verifizieren ist, steht es ausdrücklich als Verifikationsschritt.

---

## 1. Ziel und Ausgangslage

`mail-archive` kann heute genau eine Sache: eine FalkorDB starten, überwachen
und ihren Status anzeigen. Der Graph ist leer — es gibt keinen Weg, Mails
hineinzubekommen.

Diese Arbeit baut:

1. den **Import-Pfad** — Postfächer anbinden, Nachrichten abholen, kanonisch
   parsen, als Graph ablegen;
2. ein Graphmodell, das **Analysen trägt** — Ko-Empfänger, Projektzugehörigkeit,
   wiederkehrende Textbausteine;
3. eine **KI-Abfragefläche** über MCP, ohne ein Modell in den Import zu bauen.

Gmail ist der erste Anbieter. Die Struktur muss IMAP/iCloud und M365 aufnehmen,
ohne dass Engine oder Kern angefasst werden.

### 1.1 Fachliche Anforderungen an das Datenmodell

Das Graphmodell muss diese drei Fragen beantworten können:

| # | Frage | Abschnitt |
| --- | --- | --- |
| A1 | Welche Adressen werden häufig **zusammen angeschrieben**? | §6.1 |
| A2 | Welche Mails gehören vermutlich zu einem **gemeinsamen Projekt**? | §6.2 |
| A3 | Welche Mails werden regelmäßig mit **ähnlichem Wortlaut** geschrieben (→ Automatisierungspotenzial)? | §6.3 |

Zusätzlich: das Archiv soll **mit KI auswertbar, befragbar und analysierbar**
sein — als Konsument, nicht als Erzeuger (§3.2).

### 1.2 Getroffene Entscheidungen

| Frage | Entscheidung |
| --- | --- |
| Gmail-Zugriff | **Gmail REST API v1 + OAuth2** (Desktop-Client). Liefert Labels, `threadId`, `historyId` → echter Delta-Sync. Nutzer braucht ein Google-Cloud-Projekt mit OAuth-Client. |
| Body-Storage | **Plaintext im Graph (gekappt, 64 KB) + Original-`.eml` auf Platte**, content-addressed |
| Prozessmodell | **Eigener Worker-Prozess**; UI schreibt Jobs in SQLite und pollt, Worker claimt per Lease |
| KI | **Kein GraphRAG.** Modell ist KI-*auswertbar*, nicht KI-*erzeugt* |

---

## 2. Repo-Fakten (Stand der Umsetzung)

Alles hier wurde am Repo verifiziert, nicht angenommen.

### 2.1 Aufbau

```
mail-archive/
├── app/                          Reflex-Anwendung
│   ├── __init__.py               ruft configure(), exportiert `settings`
│   ├── app.py                    rx.App, Lifespan-Registrierung
│   ├── composition.py            Composition Root (graph_server, graph_status, lifespan)
│   ├── configuration.py          AppConfig(ApplicationConfig)
│   ├── components/navbar.py
│   ├── pages/{home,users}.py
│   └── states/graph_status_state.py
├── components/mailarc-core/      einziges bestehendes Sub-Projekt
│   ├── pyproject.toml
│   ├── README.md
│   ├── src/mailarc_core/{graph,database}/
│   └── tests/{graph,database}/ + test_isolation.py
├── alembic/                      env.py + versions/
├── configuration/                config.yaml + config.<profil>.yaml
├── taskfiles/                    Taskfile.{qa,db,reflex,tauri,docker,docs,release}.yml
├── spec/                         diese Datei
├── src-tauri/                    macOS-Desktop-Shell
└── pyproject.toml                Workspace-Root
```

### 2.2 Werkzeuge und Konventionen

- **Python 3.14**, `uv`, Task-Runner ist `task` (`Taskfile.dist.yml`), **nicht** `make`.
- **ruff**, `line-length = 88`, `target-version = "py314"`; Typprüfung mit `ty` (Astral).
- Befehle: `task test`, `task lint`, `task format`, `task typecheck`, `task db:upgrade`.
- Abhängigkeiten **immer** über `uv add <paket>`, nie von Hand in `pyproject.toml`.
- Coverage-Gate: **≥ 80 %** (`fail_under = 80`).
- Dateien **≤ 1000 Zeilen**.
- **Kein `print`** — `logging` mit `%s`-Platzhaltern, **keine f-Strings in
  Logger-Aufrufen** (`log.info("Loaded: %d", n)`).
- Wertobjekte sind **pydantic-Modelle, nie `@dataclass`**; unveränderliche mit
  `model_config = ConfigDict(frozen=True)`.
- Conventional Commits.

> **venv-Falle:** Das reale venv ist `.venv.mac`, nicht `.venv`. Außerhalb einer
> aktivierten Shell greift `uv` unter Umständen das falsche. Bei
> Import-Fehlern zuerst prüfen, gegen welches venv gelaufen wird.

### 2.3 Bestehende Struktur von `mailarc-core`

```
src/mailarc_core/
├── __init__.py            re-exportiert die graph-Namen
├── __main__.py            `uv run python -m mailarc_core` startet nur FalkorDB
├── graph/
│   ├── model.py           GraphServerStatus, GraphInfo, ServerMetrics,
│   │                      GraphBackend, GraphServerMode (alle frozen pydantic)
│   ├── config.py          GraphConfig(BaseConfig), env_prefix="app_graph_"
│   ├── client.py          connect()/close()/session() über runic
│   ├── admin.py           FalkorDB-only Redis-Kommandos
│   ├── runtime.py         findet vendorte redis-server + falkordb.so
│   ├── server.py          FalkorDBServer: start/adopt/stop
│   └── status.py          read_status() / read_status_async()
└── database/
    ├── __init__.py
    └── sqlite.py          prepare(), sync_database_url(), install_pragmas()
```

**Muster, die übernommen werden müssen:**

- `graph/status.py:read_status_async()` macht
  `await asyncio.to_thread(read_status, config)` mit dem Kommentar *„every runic
  driver blocks"*. **Jeder Graph-Zugriff aus async-Code folgt diesem Muster.**
- `graph/status.py:read_status()` fängt jede Exception und liefert
  `GraphServerStatus.unreachable(...)` — *„an outage is a status, not an error"*.
- `app/composition.py:graph_server()` ist `@lru_cache(maxsize=1)`, weil ein
  lokaler Server ein echter Kindprozess ist.
- `app/composition.py:graph_server_lifespan()` besitzt den Kindprozess für die
  Lebensdauer der App und **schluckt** einen Startfehler (geloggt, in
  `startup_error` hinterlegt), statt die App zu killen.

### 2.4 Architekturregeln aus `CLAUDE.md`

- `app/` darf Komponenten importieren. Eine Komponente importiert **niemals**
  `app`, Reflex oder ein `appkit`-UI-Paket.
- Innerhalb einer Komponente: `model.py` kennt kein I/O; alles andere darf es
  importieren.
- `app/composition.py` ist das **einzige** Modul, das eine Komponente aus
  Konfiguration baut. States und Pages fragen dort an, sie konstruieren nie selbst.
- Ein `Protocol` verdient seinen Platz, **wenn eine zweite Implementierung
  existiert**. Eine Implementierung hinter einem Port ist Indirektion, keine
  Architektur.

### 2.5 Bestehende Testkonventionen

- Tests spiegeln die Package-Struktur (`tests/graph/test_status.py`).
- **Kein `conftest.py`** im Repo — Fixtures liegen in den Testmodulen.
- pytest-Marker `graph_local`: *„starts the vendored FalkorDB (needs
  `task tauri:vendor`)"*.
- `asyncio_mode = "auto"` — async-Tests brauchen keinen Decorator.
- `pytest-httpserver` ist **bereits** Dev-Dependency → HTTP-Adapter werden
  dagegen getestet, nicht gegen echte APIs.
- `tests/test_isolation.py` startet einen **Subprozess** mit einem Import-Probe
  und prüft `sys.modules` gegen `FORBIDDEN = ("reflex", "appkit_mantine",
  "appkit_ui", "appkit_user")`. Subprozess, weil die App-Tests Reflex in den
  gemeinsamen Interpreter ziehen.

### 2.6 Vorbereitete, noch ungenutzte Haken

- [pyproject.toml:52](../pyproject.toml) enthält auskommentiert:
  `# mail_archive-mcp = "mail_archive.mcp_server.server:main"` → der MCP-Server
  ist im Template vorgesehen.
- `graph/model.py` definiert `MIN_VECTOR_KNN_MAJOR = 4` und
  `GraphServerStatus.vector_knn_supported` mit dem Kommentar *„Whether this
  server can serve the KNN queries the project needs"*; `app/pages/home.py`
  rendert dazu ein „KNN ready"-Badge. **Vektoren waren von Anfang an geplant.**

---

## 3. Architekturentscheidungen

### 3.1 Ein Port, kein Hexagon

Es gibt genau **eine Naht, an der Abstraktion erlaubt ist** — den Mail-Anbieter.
Alles andere ist geradlinige Schichtung. Bewusst **nicht** gebaut:

| Verworfen | Grund |
| --- | --- |
| `CredentialStore`-Port | Eine Implementierung. Der Adapter bekommt den entschlüsselten String übergeben. |
| `MailSourceFactory`-Protocol | `type MailSourceFactory = Callable[[MailAccount, str], MailSourcePort]` genügt. |
| `Domain`-Node im Graph | `Address.domain` ist indiziert und beantwortet dieselbe Frage. |
| getrennte `pipeline.py`/`engine.py` | Die Engine *ist* die Pipeline. |
| `cursor.py`, `progress.py` | Zwei Repository-Aufrufe bzw. ein Wertobjekt. |

Verwendete Muster: **Port & Adapter** (an dieser einen Naht), **Registry**,
**Repository** (appkit für SQLite, runic für Graph), **Anti-Corruption Layer**
(Provider-JSON erreicht das Graphmodell nie), **Checkpoint**.

Zwei Ports verdienen ihren Platz, weil sie ab Tag 1 mehrere Implementierungen
haben: `MailSourcePort` (Gmail, `FakeMailSource`, später IMAP/M365) und
`EmbedderPort` (Ollama lokal, OpenAI remote).

### 3.2 Kein GraphRAG — KI-auswertbar statt KI-erzeugt

`runic.rag` ist über `runic-py` installiert und böte eine komplette
GraphRAG-Pipeline (`GraphRAG`-Fassade, `PydanticAIExtractor` →
`Entity`/`RELATES_TO`-Knoten, `PydanticAISynthesizer`). **Wird nicht verwendet:**

- Bei E-Mail ist der Knowledge Graph bereits vorhanden und **exakt** — Absender,
  Empfänger, Threads, Labels, Anhänge stehen in den Headern. Eine LLM-Extraktion
  legte eine probabilistische Schicht über Grundwahrheit.
- Sie kostet einen LLM-Aufruf pro Chunk beim Import: langsam, teuer, und jede
  private Mail ginge durch ein Modell.
- Sie ist nicht reproduzierbar: zweimal importieren, zweimal ein anderer Graph.

Stattdessen:

| Bedarf | Lösung | LLM nötig? |
| --- | --- | --- |
| Ko-Empfänger (A1) | Cypher über exakte Kanten | nein |
| Textbausteine (A3) | SimHash über bereinigten Body | nein |
| Projektzuordnung (A2) | deterministische Signale + optional Vektor-Ähnlichkeit | teilweise |
| Semantische Suche | `Vector`-Property + FalkorDB-KNN | nur Embedder |
| Befragbarkeit | **MCP-Server** über katalogisierten Queries | LLM als *Konsument* |

> **Tragende Regel:** Ein LLM sieht das Archiv nur **lesend, zur Abfragezeit**.
> Nichts, was ein Modell erzeugt, wird je Grundwahrheit.
>
> Durchgesetzt durch `test_isolation.py`: **kein Modul importiert `runic.rag`.**

### 3.3 Befund: appkit-Scheduler taugt nicht als Job-Queue

Geprüft und verworfen — drei Gründe, jeder für sich ausreichend:

1. **Nicht vorhanden.** `apscheduler` ist nicht installiert;
   `from appkit_commons.scheduler import APScheduler` liefert in diesem venv
   buchstäblich `None`, weil der Import in `scheduler/__init__.py` in einem
   `try/except ImportError` steckt. `PGQueuerScheduler` ebenso.
2. **PostgreSQL-gebunden.** `APScheduler._configure_scheduler()` baut fest einen
   `PsycopgEventBroker(conninfo=config.url)`. Mit `sqlite+aiosqlite:///…`
   scheitert der Konstruktor, das umgebende `except Exception` fängt es und
   **fällt still auf ein In-Memory-`AsyncScheduler()` zurück** — Persistenz und
   prozessübergreifende Koordination gehen verloren, ohne dass etwas rot wird.
3. **Ein Cron, keine Work-Queue.** Das `Scheduler`-ABC kennt nur
   `add_service(ScheduledService)` mit `trigger`. Kein Einzel-Enqueue, kein
   Fortschritt pro Job, kein Cancel, kein Lease.

**Folge:** eigene Jobtabelle (§7.2). Für den wiederkehrenden *Auslöser* in
Phase 7 bleibt der Scheduler eine Option — erst bei einem PostgreSQL-Deployment.

---

## 4. Modulstruktur

### 4.1 Hierarchie und erlaubte Importe

```
app/                          Pages, Routen, Composition Root, Worker, MCP-Server
 ├─ mailarc-ui                Reflex-Komponenten + States (EINZIGES Modul mit Reflex)
 │   ├─ mailarc-sync          Engine, Job-Queue, Worker-Loop, ProviderRegistry
 │   │   └─ mailarc-core      Domäne, Port, Graph-Grundwahrheit, SQLite, BlobStore
 │   └─ mailarc-analytics     abgeleitete Knoten, Analyse-Queries, Embeddings
 │       └─ mailarc-core
 └─ mailarc-google            Gmail-Adapter — hängt NUR an mailarc-core
```

| Modul | darf importieren | darf **nicht** |
| --- | --- | --- |
| `mailarc-core` | appkit-commons, runic.ogm, pydantic, sqlalchemy | Reflex, Provider, `runic.rag` |
| `mailarc-google` | `mailarc-core`, httpx, google-auth | `mailarc-sync`, Reflex, `runic.rag` |
| `mailarc-sync` | `mailarc-core` | `mailarc_google`, Reflex, `runic.rag` |
| `mailarc-analytics` | `mailarc-core` | `mailarc-sync`, Reflex, `runic.rag` |
| `mailarc-ui` | `mailarc-core`, `-sync`, `-analytics`, reflex, appkit-mantine/-user | `runic.rag` |
| `app` | alles | `runic.rag` |

**`app` ist der einzige Ort, der konkrete Implementierungen kennt.** Die
Registrierung `registry.register(GmailSource.DESCRIPTOR, GmailSource.create)`
passiert in `app/composition.py`. Auch der Worker-Entrypoint liegt in
`app/worker.py` — sonst müsste `mailarc-sync` die Provider kennen.

> Verifiziert: `app.configuration` zieht **kein** Reflex nach
> (`appkit_user.configuration` importiert es nicht). Der Worker-Prozess bleibt
> damit schlank, obwohl er `app.*` importiert.

### 4.2 Sub-Projekt-Layout (verbindlich für alle fünf)

```
components/<modul>/
├── pyproject.toml     eigener [project]-Block, eigene dependencies, hatchling
├── README.md          wofür das Modul da ist und was es NICHT darf
├── src/<paket>/       src-Layout
└── tests/             eigene Testsuite, Struktur spiegelt src/
```

`pyproject.toml`-Vorlage (nach `components/mailarc-core/pyproject.toml`):

```toml
[project]
name = "mailarc-<x>"
version = "1.0.0"
description = "…"
readme = "README.md"
requires-python = ">=3.14"
license = { text = "MIT" }
authors = [{ name = "Jens Rehpöhler" }]
dependencies = ["mailarc-core"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/mailarc_<x>"]

[tool.pytest.ini_options]
cache_dir = "../../.cache/pytest"
testpaths = ["tests"]
asyncio_mode = "auto"
```

Im Root-`pyproject.toml` je Modul zu ergänzen:

- `[tool.uv.sources]`: `mailarc-<x> = { workspace = true }`
- `[tool.pytest.ini_options] testpaths`
- `[tool.coverage.run] source`
- `[tool.ruff.lint.isort] known-local-folder`

`[tool.uv.workspace] members = ["components/*"]` greift bereits.

> **In Phase 0 zu verifizieren, nicht anzunehmen:** ob `[tool.uv.sources]` im
> Workspace-Root auch für Member gilt oder ob jedes Member seine eigenen
> Sources deklarieren muss, damit `mailarc-sync` → `mailarc-core` auflöst.
> `uv sync` und ein Import-Smoke-Test entscheiden das. Bei Zweifel: Sources
> zusätzlich im Member deklarieren — das funktioniert in beiden Fällen.

### 4.3 Einheitliche Package-Struktur

Jedes Modul gliedert sich in **Capability-Packages** mit identischen
Dateirollen. Die Konvention existiert bereits in `graph/` und wird überall
angewandt:

| Datei | Rolle | Pflicht |
| --- | --- | --- |
| `__init__.py` | öffentliche Oberfläche, Re-Exports | ja |
| `model.py` | Wertobjekte / OGM-Knoten, **kein I/O** | ja |
| `config.py` | `BaseConfig` dieser Capability | wenn konfigurierbar |
| `ports.py` | `Protocol`s | nur bei zweiter Implementierung |
| übrige | Verhalten, ein Modul je Verantwortung | — |

```
mailarc-core/src/mailarc_core/
  graph/       model config client admin runtime server status      (bestehend)
  database/    sqlite entities repositories                         (erweitert)
  mail/        model config ports identity parsing errors
  archive/     model config blobs writer

mailarc-sync/src/mailarc_sync/
  engine/      model config engine registry
  jobs/        model queue worker

mailarc-analytics/src/mailarc_analytics/
  derived/     model config correspondents topics templates
  semantic/    model config ports embedder search
  queries/     model catalog

mailarc-google/src/mailarc_google/
  source/      model config credentials oauth client mapping

mailarc-ui/src/mailarc_ui/
  accounts/    state components
  imports/     state components
  insights/    state components
```

Das Provider-Package heißt `source/` nach der **Fähigkeit**, nicht nach dem
Anbieter — `mailarc_google.source`, `mailarc_imap.source`, `mailarc_m365.source`
sehen identisch aus, statt dass `mailarc_google.gmail` stottert.

### 4.4 Anpassung der Isolationsregel

`mailarc-ui` *muss* Reflex importieren. `components/mailarc-core/tests/test_isolation.py`
wird umgebaut:

- Prüft `mailarc_core`, `mailarc_sync`, `mailarc_analytics`, `mailarc_google`
  gegen `FORBIDDEN = ("reflex", "appkit_mantine", "appkit_ui", "appkit_user")`.
- Prüft **zusätzlich** für alle Module inkl. `mailarc_ui`, dass `runic.rag`
  nicht importiert wird.
- Nimmt `mailarc-ui` von der Reflex-Prüfung ausdrücklich aus.
- Bleibt ein Subprozess-Probe (Begründung s. §2.5).

`CLAUDE.md` §6 wird entsprechend umformuliert.

---

## 5. Graphmodell

### 5.1 Grundwahrheit — geschrieben **nur** von `mailarc-core/archive/`

Jedes Feld stammt aus der Nachricht selbst. Nichts ist geraten.

| Node | PK | Kernfelder |
| --- | --- | --- |
| `Message` | kanonische ID (§5.3) | `rfc_message_id`, `subject` (FULLTEXT), `subject_norm` (index), `sent_at` (index), `body_text` (FULLTEXT, gekappt), `body_clean`, `simhash` (index), `participant_key` (index), `refs`, `size_bytes`, `has_attachments`, `eml_sha256`, `embedding` (VECTOR, optional), `embedding_model` |
| `Address` | normalisierte Adresse (lowercase) | `local_part`, `domain` (index), `display_names` |
| `Thread` | `{account}:{provider_thread_id}` | `subject` |
| `Label` | `{account}:{name}` | `name` (index), `kind` (system/user/folder) |
| `Attachment` | `sha256` | `content_type`, `size` |
| `Account` | SQLite-Konto-ID als str | `address` (index), `provider` |

**Die fünf analysetragenden Felder auf `Message` — alle entstehen beim Import,
nicht nachträglich:**

| Feld | Inhalt | trägt |
| --- | --- | --- |
| `subject_norm` | `Re:`/`AW:`/`Fwd:`/`WG:` und Ticket-Token entfernt, lowercase | A2 |
| `participant_key` | sha256 über die **sortierte Menge** aller beteiligten Adressen | A1, A2 |
| `simhash` | 64-Bit-SimHash über 3-Wort-Shingles von `body_clean` | A3 |
| `refs` | Ticket-/Vorgangs-Token (`[PROJ-123]`, `#4711`) aus Betreff und Body | A2 |
| `body_clean` | Body **ohne** zitierte Vorgänger, Signatur, Disclaimer | A3, Embedding |

> **`body_clean` ist Voraussetzung, kein Komfort.** Ohne diesen Schritt sieht
> jede Mail mit derselben Firmen-Fußzeile wie ein Textbaustein aus und A3
> liefert Müll. `body_text` (voll, für Volltextsuche) und `body_clean`
> (bereinigt, für Hash und Embedding) sind **zwei verschiedene Felder**.

**Edges:**

```
(Message)-[:SENT_FROM]->(Address)
(Message)-[:SENT_TO]->(Address)
(Message)-[:COPIED_TO]->(Address)          # Cc
(Message)-[:BLIND_COPIED_TO]->(Address)    # Bcc
(Message)-[:IN_THREAD]->(Thread)
(Message)-[:REPLIES_TO]->(Message)         # aus In-Reply-To
(Message)-[:HAS_ATTACHMENT {filename, content_id, inline}]->(Attachment)
(Message)-[:LABELED]->(Label)
(Message)-[:ARCHIVED_FROM {provider_message_id, provider_thread_id, folder, uid, archived_at}]->(Account)
```

Begründungen:

- **Getrennte Edge-Typen für To/Cc/Bcc** statt eines `role`-Properties: die
  Menge ist durch RFC 5322 geschlossen, und die Ko-Empfänger-Abfrage (A1)
  braucht dann kein `optional=False` plus Edge-Property-Filter.
- **`SENT_FROM` statt `FROM`** — `FROM` ist in Cypher-Dialekten heikel.
- **`Attachment` content-addressed:** dieselbe Datei an zwei Mails ist *ein*
  Node, der Dateiname hängt an der Kante. Fällt als Projektsignal ab (A2).
- **`ARCHIVED_FROM` trägt die Provenienz:** dieselbe Mail über zwei Konten →
  *ein* `Message`-Node, zwei Kanten.

### 5.2 Abgeleitetes — geschrieben **nur** von `mailarc-analytics/derived/`

Jeder dieser Knoten ist **jederzeit löschbar und neu berechenbar**.

| Node | PK | Felder |
| --- | --- | --- |
| `Group` | `participant_key` | `size`, `message_count`, `first_seen`, `last_seen` |
| `Topic` | ULID | `label`, `method`, `score`, `message_count`, `first_seen`, `last_seen` |
| `Template` | `simhash`-Bucket | `sample_text`, `occurrences`, `first_seen`, `last_seen`, `automation_score` |

```
(Address)-[:CO_ADDRESSED {count, first_seen, last_seen}]-(Address)   # ungerichtet
(Message)-[:ADDRESSED_GROUP]->(Group)
(Message)-[:ABOUT {score, method}]->(Topic)
(Message)-[:INSTANCE_OF {distance}]->(Template)
```

> **Zentrale Modellierungsdisziplin:** Ein abgeleiteter Knoten wird **nie** zur
> Grundwahrheit. `Label` kommt vom Anbieter, `Topic` von uns — sie werden nie
> vermischt. Jede abgeleitete Kante trägt `method` und `score`, damit
> nachvollziehbar bleibt, *warum* sie da ist, und ein Mensch sie verwerfen kann.

`task graph:rebuild-derived` löscht alle vier Typen und rechnet sie neu — ein
Analyse-Bug kostet einen Lauf, keine Migration.

### 5.3 Kanonische Nachrichten-Identität (`mail/identity.py`)

```
canonical_id = normalisierte RFC5322 Message-ID   (<> entfernt, Domain lowercase)
             | falls fehlend/leer:
               "sha256:" + sha256(sent_at | from | subject | sha256(body_bytes))
```

**Vertrag:** Zweimaliger Import derselben Mailbox erzeugt **null** neue Nodes
und **null** neue Kanten. Als Test festgeschrieben, nicht als Kommentar.

---

## 6. Die drei Analysen

### 6.1 A1 — Welche Adressen werden häufig zusammen angeschrieben?

Direkt auf der Grundwahrheit, ohne Vorberechnung:

```cypher
MATCH (a:Address)<-[:SENT_TO|COPIED_TO]-(m:Message)-[:SENT_TO|COPIED_TO]->(b:Address)
WHERE a.address < b.address
RETURN a.address, b.address, count(m) AS together
ORDER BY together DESC LIMIT 25
```

Ab etwa 100k Nachrichten wird der Selbstjoin teuer → `CO_ADDRESSED`-Kante
materialisieren, inkrementell beim Import fortgeschrieben.

Für „welche **Gruppe** schreibt wiederholt miteinander" (nicht nur Paare) ist
`participant_key` der bessere Weg — ein `GROUP BY` statt einer Cliquensuche:

```cypher
MATCH (m:Message)-[:ADDRESSED_GROUP]->(g:Group)
WHERE g.size > 2 AND g.message_count > 5
RETURN g.key, g.size, g.message_count ORDER BY g.message_count DESC
```

### 6.2 A2 — Welche Mails gehören zu einem gemeinsamen Projekt?

Bewusst **hybrid** und nach Verlässlichkeit gestaffelt. Reine Embedding-Cluster
sind hier deutlich schlechter als die exakten Signale:

| Rang | Signal | Quelle | Art |
| --- | --- | --- | --- |
| 1 | Ticket-/Vorgangs-Token | `Message.refs` | exakt |
| 2 | Thread-Zugehörigkeit | `IN_THREAD` | exakt |
| 3 | normalisierter Betreff | `subject_norm` | exakt |
| 4 | geteilter Anhang-Hash | `HAS_ATTACHMENT` | exakt |
| 5 | wiederkehrende Teilnehmergruppe | `participant_key` | exakt |
| 6 | semantische Nähe | `embedding` + KNN | unscharf |

Aus 1–5 wird ein Ähnlichkeitsgraph gebaut, per Connected Components über einem
Schwellwert geclustert und als `Topic` zurückgeschrieben. Signal 6 verbindet
**nur**, was 1–5 offen gelassen haben.

`ABOUT.method` hält fest, welches Signal die Kante gezogen hat: ein
`method="embedding"`-Cluster ist ein **Vorschlag**, ein `method="ref"`-Cluster
eine **Tatsache**. Der Nutzer kann ein `Topic` in ein echtes `Label` befördern;
die Rückrichtung gibt es nicht.

### 6.3 A3 — Welche Mails werden regelmäßig mit ähnlichem Wortlaut geschrieben?

**Lexikalisch, nicht semantisch.** Embeddings sind hier das falsche Werkzeug:
sie finden Mails über *dasselbe Thema*, gesucht sind Mails mit *demselben
Wortlaut*.

1. `body_clean` (Zitate/Signatur/Disclaimer entfernt) → 3-Wort-Shingles.
2. 64-Bit-SimHash → `Message.simhash`.
3. Bucketing über 4 × 16-Bit-Bänder (LSH), danach Hamming-Distanz ≤ 3 innerhalb
   des Buckets. Linear statt quadratisch. FalkorDB kann Hamming nicht selbst —
   die Gruppierung läuft in Python über die indizierte Spalte.
4. Gruppe ≥ 3 Vorkommen → `Template`-Knoten.

`automation_score` gewichtet **Häufigkeit × Regelmäßigkeit der Abstände ×
Kürze**: eine monatlich fast wortgleiche Statusmail ist ein besserer
Automatisierungskandidat als 200 identische Newsletter-Eingänge.

Empfangene und selbst gesendete Mails werden **getrennt** ausgewiesen —
automatisierbar ist nur, was man selbst schreibt.

---

## 7. Technische Bausteine

### 7.1 `MailSourcePort` (`mailarc_core/mail/ports.py`)

```python
class MailSourcePort(Protocol):
    provider: MailProvider

    async def verify(self) -> AccountIdentity: ...
    async def list_labels(self) -> Sequence[LabelInfo]: ...
    async def list_messages(
        self, cursor: SyncCursor | None, *, limit: int
    ) -> MessagePage: ...
    async def fetch_raw(
        self, refs: Sequence[MessageRef]
    ) -> AsyncIterator[RawMessage]: ...
    async def aclose(self) -> None: ...
```

- `MessagePage(refs, next_cursor, estimated_total)` — Paginierung ist Sache des
  Adapters; die Engine sieht nur „noch eine Seite".
- `SyncCursor(provider, token, kind: full|incremental)` ist für die Engine
  **opak**: Gmail legt `historyId` hinein, IMAP `UIDVALIDITY/UIDNEXT`, MS Graph
  den `deltaLink`.
- Async, weil ein Erstimport zehntausende HTTP-Requests bedeutet. Die
  **runic-Seite bleibt synchron** und läuft über `asyncio.to_thread` (§2.3).
  Grund: `AsyncSession` kann kein Lazy-Loading und wirft `LazyLoadError`.

Dazu `ProviderDescriptor(provider, label, credential_fields, supports_incremental)`
— speist die Registry **und** das UI-Formular.

### 7.2 Job-Queue (`mailarc_sync/jobs/queue.py`)

```
queued ──claim──> running ──> succeeded
                     │└──────> failed        (mit error)
                     └────────> cancelled    (cancel_requested)
```

- **Claim** = konditionales
  `UPDATE … SET state='running', worker_id=?, lease_until=? WHERE id=? AND state='queued'`,
  Erfolg über `rowcount`. SQLite hat kein `SKIP LOCKED`, Compare-and-Swap genügt.
  WAL, `busy_timeout=5000` und `foreign_keys=ON` sind über
  `mailarc_core.database.sqlite.install_pragmas()` bereits gesetzt und gelten in
  **beiden** Prozessen.
- **Heartbeat** alle 10 s verlängert `lease_until`; ein abgelaufener Lease wird
  vom nächsten Worker-Start neu geclaimt → Wiederaufnahme am Checkpoint.
- **Cancel** setzt `cancel_requested`; der Worker prüft zwischen Batches.
- `kind` ∈ `import` | `incremental` | `derive` | `embed`. Die Zuordnung
  kind → Handler passiert in `app/worker.py` — dieselbe Composition-Root-Regel,
  keine neue Abstraktion.

### 7.3 Engine (`mailarc_sync/engine/engine.py`)

```
list_messages(cursor)
  → refs ohne bereits archivierte (Batch-SELECT auf mail_archived_messages)
    → fetch_raw(batch)         Semaphore, Default 8 parallel
      → parse + clean          stdlib email, in to_thread (CPU-gebunden)
        → BlobStore.put        .eml + Anhänge, sha256
          → MessageArchiver    runic Session, in to_thread, EIN Consumer
            → Checkpoint alle 200 Nachrichten
```

`asyncio.Queue` zwischen Fetch- und Archivstufe gibt Backpressure. Genau **ein**
Archiv-Consumer: FalkorDB-Schreibzugriffe zu serialisieren ist billiger, als sie
zu koordinieren.

### 7.4 `EmbedderPort` (`mailarc_analytics/semantic/ports.py`)

```python
class EmbedderPort(Protocol):
    model: str
    dimension: int

    async def embed(self, texts: Sequence[str]) -> Sequence[Vector]: ...
```

Zwei Implementierungen ab Tag 1 — deshalb verdient dieser Port seinen Platz:
`OllamaEmbedder` (lokal, `nomic-embed-text`, 768) und `OpenAIEmbedder`
(`text-embedding-3-small`, 1536). Eigener Code; **`runic.rag` wird nicht
importiert** (§3.2).

**Default ist `none`:** ohne konfigurierten Embedder laufen A1–A3 vollständig,
nur semantische Suche und Signal 6 aus A2 fehlen. Das hält die Desktop-App frei
von Voraussetzungen.

> **Falle:** Der FalkorDB-VECTOR-Index ist auf eine **feste Dimension**
> migriert. Ein Embedder-Wechsel erfordert neuen Index **und** Neuberechnung
> aller Vektoren. Deshalb steht `Message.embedding_model` am Knoten — ein
> Wechsel wird so erkennbar und die Neuberechnung gezielt begrenzbar.

### 7.5 Befragbarkeit: MCP statt eingebautem LLM

`app/mcp_server/` aktiviert den in `pyproject.toml` bereits vorgesehenen
Entrypoint (§2.6) und stellt bereit: `search_messages` (Volltext + optional
KNN), `co_recipients`, `topics`, `templates`, `thread`, `timeline`.

Jedes Werkzeug ist ein parametrisiertes, katalogisiertes Cypher aus
`mailarc_analytics/queries/catalog.py` — **kein freies Cypher von außen**.

### 7.6 Fehlertaxonomie (`mailarc_core/mail/errors.py`)

| Fehler | Reaktion |
| --- | --- |
| `MailAuthError` | Job → `failed`, `account.status = auth_error`, UI bietet Re-Consent |
| `MailTransientError` (429, 5xx, Netz) | Backoff + Jitter, `Retry-After` respektiert |
| `MailPermanentError` (kaputte MIME, 404) | Nachricht überspringen, Zeile in `mail_failed_messages`, weiter |

**Kein `except: pass`.** Jede übersprungene Nachricht hinterlässt eine Zeile.

---

## 8. Relationales Schema und Blob-Store

### 8.1 Tabellen (`mailarc_core/database/entities.py`)

| Tabelle | Zweck |
| --- | --- |
| `mail_accounts` | `provider`, `display_name`, `email_address`, `enabled`, `status`, `last_sync_at`, `last_error`; `UNIQUE(provider, email_address)` |
| `mail_credentials` | `account_id` (FK), `kind`, `secret: EncryptedString` |
| `mail_sync_checkpoints` | `account_id`, `scope`, `cursor`, `last_run_at`, `messages_seen` |
| `mail_sync_jobs` | `kind`, `state`, `worker_id`, `lease_until`, `heartbeat_at`, `cancel_requested`, Fortschrittszähler, `error` |
| `mail_archived_messages` | `account_id`, `provider_message_id`, `canonical_id`, `archived_at` |
| `mail_failed_messages` | `account_id`, `provider_message_id`, `reason`, `detail`, `occurred_at` |

Zwei Punkte, die Aufmerksamkeit verdienen:

- **`mail_credentials.secret` ist absichtlich strukturlos.** Jeder Provider
  serialisiert sein eigenes Pydantic-Modell hinein (`GmailCredentials` mit
  `client_id`, `client_secret`, `refresh_token`, …). Ein neuer Anbieter braucht
  damit **keine Migration** — der ACL an der Persistenzgrenze.
- **`mail_archived_messages` ist ein bewusstes Read-Model.** Der Graph kann
  „kenne ich Provider-ID X für Konto Y?" nicht batchweise billig beantworten;
  die relationale Seite kann es mit `IN (…)` pro Batch. Ein Rebuild-Pfad aus dem
  Graph wird mitgeliefert, damit die Tabelle jederzeit verwerfbar bleibt.

### 8.2 Blob-Store (`mailarc_core/archive/blobs.py`)

```
.state/mailstore/<aa>/<bb>/<sha256>.eml       Original RFC822
.state/mailstore/<aa>/<bb>/<sha256>.bin       Anhang
```

Content-addressed, write-once, per `os.replace` atomar. Pfad und
Body-Kappungsgrenze (Default 64 KB) aus `archive/config.py`.

---

## 9. Benötigte Bibliotheks-APIs

Damit die Umsetzung nichts nachschlagen muss.

### 9.1 appkit-commons — Persistenz

```python
from appkit_commons.database.entities import Base, Entity, EncryptedString, ArrayType
from appkit_commons.database.base_repository import BaseRepository
from appkit_commons.database.session import get_asyncdb_session
from appkit_commons.registry import service_registry
from appkit_commons.configuration.base import BaseConfig
```

`Entity` (Mixin) liefert automatisch: `id: int` (PK, autoincrement, indiziert),
`created: datetime` (UTC, `server_default`), `updated: datetime` (auto on update).

```python
class MailAccountEntity(Entity, Base):
    __tablename__ = "mail_accounts"
    provider: Mapped[str] = mapped_column(String(32))
    secret: Mapped[str] = mapped_column(EncryptedString)  # Fernet at rest


class MailAccountRepository(BaseRepository[MailAccountEntity]):
    @property
    def model_class(self) -> type[MailAccountEntity]:
        return MailAccountEntity
```

`BaseRepository` bringt mit: `create`, `find_by_id`, `find_all`,
`find_all_by_ids`, `exists_by_id`, `count`, `update`, `save`, `save_all`,
`delete`, `delete_by_id`, `delete_all_by_ids`, `delete_all`.
**Nur eigene Queries ergänzen, CRUD nicht duplizieren.**

```python
async with get_asyncdb_session() as session:
    account = await repo.create(session, MailAccountEntity(...))
# committet beim Verlassen automatisch, rollback bei Exception
```

**Anti-Patterns (harte Regeln):**

| Falsch | Richtig |
| --- | --- |
| `rx.asession()` | `get_asyncdb_session()` |
| `service_registry()` auf Modulebene | innerhalb von Funktionen/Methoden |
| SQLAlchemy-Entities im Reflex-State | Pydantic-DTO über `to_dict()` |
| `session.commit()` im `get_asyncdb_session()`-Block | Auto-Commit beim Exit |
| `alembic --autogenerate` | Migration **von Hand** schreiben |

`EncryptedString` liest den Schlüssel zur Laufzeit aus
`DatabaseConfig.encryption_key` (in `configuration/config.yaml` als
`secret:mn-db-encryption-key`). Ohne gesetzten Schlüssel wirft der erste
Schreibzugriff.

### 9.2 Alembic

`down_revision` ist der **`revision`-String der Vorgängerdatei**, nicht deren
Dateiname. Vorgänger hier:
`alembic/versions/2025_12_30_appkit_user_0.10.0.py` — deren `revision`-Wert
öffnen und kopieren. Der `# Revises:`-Kommentar im Modul-Docstring muss dazu
passen.

`alembic/env.py` importiert `Base` aus `appkit_commons.database.entities` und
setzt `target_metadata = [Base.metadata]`. **Neue Entities werden nur gesehen,
wenn ihr Modul importiert wird** — `alembic/env.py` braucht deshalb ein
`from mailarc_core.database import entities  # noqa: F401`.

Migrationen laufen über `task db:upgrade` (→ `uv run alembic upgrade head`).

### 9.3 runic.ogm — Graph

```python
from runic.ogm import Node, Edge, Field, Relation, Session, Repository, select, Vector
```

```python
class Message(Node, labels=["Message"]):
    id: str = Field(primary_key=True)
    subject: str | None = Field(default=None, index_type="FULLTEXT")
    sent_at: datetime | None = Field(default=None, index=True)
    embedding: Vector | None = Field(default=None, index_type="VECTOR")


class ArchivedFrom(Edge, type="ARCHIVED_FROM"):
    provider_message_id: str
    archived_at: datetime
```

- Konstruktoren sind **keyword-only**.
- `datetime`, `Enum`, `Vector` bekommen ihren `TypeConverter` automatisch —
  **kein** `converter=` angeben.
- `session.relate(a, A.rel, b, edge=EdgeModel(...))` hat **MERGE-Semantik**:
  idempotent, und ein erneuter Aufruf **aktualisiert** die Edge-Properties.
  Genau das trägt den Idempotenz-Vertrag aus §5.3.
- Indexe werden am Modell **deklariert**, aber von `runic.migrate` **erzeugt**.
- Zugriff im Repo über `mailarc_core.graph.client.session(config)` — der
  Kontextmanager schließt Session **und** Driver.
- Alle Driver **blockieren** → aus async-Code immer `asyncio.to_thread`.

### 9.4 runic.migrate — Graph-Migrationen

```bash
runic init                    # legt runic/env.py + versions/ an
runic revision -m "message"
runic upgrade head
runic current | history | heads
```

`runic/env.py`:

```python
from runic.migrate import context
from runic.migrate.adapters import create_adapter

adapter = create_adapter(
    "falkordb", url="falkor://localhost:6379", graph_name="mail-archive"
)
context.configure(adapter)
```

Migrationsdatei: `revision`, `down_revision`, `upgrade(op)`, `downgrade(op)`.
Regeln: Range-Index **vor** Unique-Constraint anlegen, Constraint **vor** Index
droppen, Datenschritte **idempotent** (`MERGE`), `irreversible = True` für
Einwegschritte, `snapshot = True` für riskante Datenmigrationen.

Ein `task graph:*`-Namespace analog zu `taskfiles/Taskfile.db.yml` wird ergänzt.

### 9.5 Reflex / appkit_mantine — UI

```python
import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.templates import authenticated, navbar_layout
```

- Seiten-Factory-Muster siehe `app/pages/users.py` (`@authenticated(route=…,
  navbar=…, admin_only=…)`) und `app/pages/home.py` (`@navbar_layout`).
- State-Vars müssen serialisierbar sein → **pydantic `BaseModel`**, nicht
  `rx.Base` (in Reflex 0.9 entfernt). Reflex löst `row.field` in `rx.foreach`
  auf `BaseModel` korrekt auf.
- Hintergrundarbeit: `@rx.event(background=True)` mit `async with self:` **nur**
  um die State-Mutation, `await asyncio.sleep(...)` außerhalb des Locks —
  Vorlage: `app/states/graph_status_state.py:poll`.
- Verfügbare Komponenten u. a.: `text_input`, `password_input`, `select`,
  `number_input`, `switch`, `button`, `card`, `stack`, `group`, `table`,
  `data_list`, `alert`, `badge`, `progress`, `modal`, `drawer`, `empty_state`,
  `form`, `fieldset`, `stepper`, `tabs`, `loading_overlay`.

### 9.6 Neue Abhängigkeiten

Jeweils in der Phase, die sie einführt, per `uv add` — nicht vorab:

| Phase | Paket | Modul |
| --- | --- | --- |
| 3 | `google-auth`, `google-auth-oauthlib` | mailarc-google |
| 3 | `httpx` (bereits im venv, explizit deklarieren) | mailarc-google |
| 5 | ggf. `networkx` für Connected Components | mailarc-analytics |
| 6 | MCP-SDK | app |

---

## 10. Phasenplan

Jede Phase endet grün: `task format && task lint && task typecheck && task test`.

### Phase 0 — Sub-Projekte anlegen

**Tun**

1. `components/mailarc-{sync,analytics,google,ui}/` nach §4.2 anlegen
   (pyproject.toml, README.md, `src/<paket>/__init__.py`, `tests/`).
2. Root-`pyproject.toml`: `[tool.uv.sources]`, `testpaths`,
   `[tool.coverage.run] source`, `[tool.ruff.lint.isort] known-local-folder`.
3. `test_isolation.py` nach §4.4 umbauen (vier Module + `runic.rag`-Verbot,
   `mailarc-ui` ausgenommen).
4. `CLAUDE.md` §6 an die neue Modulhierarchie anpassen.

**DoD**

- `uv sync` löst alle fünf Module auf.
- **Verifiziert**, ob Root-`[tool.uv.sources]` für Member gilt (§4.2) — ein
  Smoke-Test `python -c "import mailarc_sync, mailarc_core"` beweist es.
- `task test` grün; `test_isolation` schlägt fehl, wenn man testweise
  `import reflex` in `mailarc_sync/__init__.py` schreibt.

### Phase 1 — Core: Domäne, Persistenz, Grundwahrheit

**Tun**

1. `mail/`: `model.py` (MailProvider, EmailAddress, RawMessage, ParsedMessage,
   MessageRef, MessagePage, SyncCursor, LabelInfo, AccountIdentity),
   `identity.py` (§5.3), `parsing.py` (RFC5322 → ParsedMessage inkl.
   `body_clean`, `subject_norm`, `participant_key`, `simhash`, `refs`),
   `ports.py` (§7.1), `errors.py` (§7.6), `config.py`.
2. `archive/`: `model.py` (runic-Knoten §5.1), `writer.py` (idempotenter Upsert),
   `blobs.py` (§8.2), `config.py`.
3. `database/entities.py` + `repositories.py` (§8.1, §9.1).
4. `alembic/env.py` um den Entities-Import ergänzen; Migration **von Hand**
   (§9.2).
5. `runic init` + Baseline-Migration: unique `Message.rfc_message_id`, FULLTEXT
   auf `subject` und `body_text`, Range-Index auf `sent_at`, `subject_norm`,
   `simhash`, `participant_key`, `Address.domain`. `task graph:*`-Namespace.

**DoD**

- Parser-, Identity- und SimHash-Tests laufen synchron **ohne I/O**.
- Writer-Test unter Marker `graph_local`.
- **Idempotenz bewiesen:** zweimaliges Archivieren derselben `.eml` ändert Node-
  und Edge-Zahlen nicht.
- **`body_clean` bewiesen:** ein Fixture mit Firmen-Fußzeile und zitiertem
  Vorgänger zeigt, dass beide entfernt werden.
- Graph-Größe pro 10.000 Testnachrichten gemessen und im README notiert.

### Phase 2 — Engine + Worker

**Tun**

1. `mailarc_sync/engine/`: `registry.py`, `engine.py` (§7.3), `model.py`,
   `config.py`.
2. `mailarc_sync/jobs/`: `model.py`, `queue.py` (§7.2), `worker.py` (Poll-Loop
   ohne Prozessverwaltung und ohne Config-Bau).
3. `FakeMailSource` (In-Memory über `.eml`-Fixtures) als **zweite**
   Port-Implementierung.
4. `app/worker.py` + `python -m app.worker`; `task sync:worker`.
5. `app/composition.py`: `sync_worker_lifespan()` startet und überwacht den
   Worker als Kindprozess — dieselbe Mechanik wie `graph_server_lifespan`,
   schaltbar über `sync.supervise_worker` (aus für Docker/systemd).

**DoD**

- Job über die Repository-API einreihen → Worker importiert die Fixtures.
- `kill -9` mitten im Lauf → Neustart claimt den abgelaufenen Lease und nimmt am
  Checkpoint auf, **ohne Duplikate**.
- Cancel bricht zwischen zwei Batches ab.

### Phase 3 — Gmail

**Tun**

1. `mailarc_google/source/`: `credentials.py`, `oauth.py` (Installed-App-Consent
   mit Loopback-Redirect), `client.py` (httpx `AsyncClient` gegen
   `gmail.googleapis.com`), `mapping.py`, `model.py`, `config.py`.
2. Nachrichten **immer** als `format=raw` holen — ein Parser bedient jeden
   Provider, der Adapter bleibt dünn. Labels, `threadId`, `historyId` kommen als
   Metadaten daneben.
3. Token-Refresh (blockierend) in `to_thread`; neues Refresh-Token zurück in
   `mail_credentials`.

**DoD**

- Adapter-Tests **vollständig** gegen `pytest-httpserver`, inkl. 429 mit
  `Retry-After` und abgelaufenem Token. **Kein Test spricht mit Google.**

### Phase 4 — UI + Verdrahtung

**Tun**

1. `mailarc_ui/accounts/` und `imports/` (state + components). Formularfelder aus
   `ProviderDescriptor.credential_fields` per `rx.foreach` — ein neuer Provider
   braucht keine UI-Zeile.
2. `app/pages/mail_accounts.py` unter `/mail/accounts`, `@authenticated`-Layout
   wie `app/pages/users.py`; Link in `app/components/navbar.py`.
3. `app/configuration.py`: `AppConfig` um `sync`, `archive`, `google` erweitern.
   `app/composition.py` registriert `GmailSource` in der Registry.

**Bewusst hässlich** — nur Konto anlegen, Consent auslösen, Import starten,
Fortschritt pollen, abbrechen. Wird später ersetzt.

**DoD**

- Konto anlegen → Import starten → Fortschritt läuft hoch → Graph zeigt die
  Nodes auf der Home-Seite.
- State-Tests nach dem `reflex-testing-state`-Muster.

### Phase 5 — Deterministische Analysen (A1–A3)

**Tun**

1. `mailarc_analytics/derived/`: `correspondents.py` (`CO_ADDRESSED`, `Group`),
   `topics.py` (Signale 1–5 → `Topic`), `templates.py` (SimHash-LSH →
   `Template`), `model.py`, `config.py`.
2. `mailarc_analytics/queries/catalog.py` — parametrisierte Cypher-Queries.
3. Job-`kind=derive`; `task graph:rebuild-derived`.

**DoD**

- Fixture-Korpus mit **gepflanzten** Fällen: eine Projektkorrespondenz, eine
  monatlich wiederkehrende Statusmail, eine Newsletter-Serie. Jede Analyse
  findet genau das Gepflanzte.
- **Kein Embedder beteiligt.**
- `rebuild-derived` ist idempotent: zweimal laufen lassen ändert nichts.

### Phase 6 — Semantik + Befragbarkeit

**Tun**

1. `mailarc_analytics/semantic/`: `ports.py` (§7.4), `embedder.py` (Ollama +
   OpenAI), `search.py` (KNN + Volltext), Job-`kind=embed`.
2. VECTOR-Index-Migration mit fester Dimension; `embedding_model` am Knoten.
3. Signal 6 in `topics.py` ergänzen (verbindet nur, was 1–5 offen ließen).
4. `app/mcp_server/` mit dem Query-Katalog (§7.5); Entrypoint in
   `pyproject.toml` aktivieren. `insights/`-Seite in der UI.

**DoD**

- Bei ausgeschaltetem Embedder liefert die semantische Suche eine **klare
  Fehlermeldung**, kein leeres Ergebnis.
- Alle Phase-5-Analysen laufen unverändert weiter.
- MCP-Werkzeuge sind aus einem MCP-Client aufrufbar.

### Phase 7 — Inkrementell + Zeitplan

**Tun**

1. `kind=incremental` über Gmails `history.list`; bei `historyId`-Lücke Fallback
   auf Vollabgleich.
2. Auslöser: `asyncio`-Intervallschleife im Worker, die Jobs einreiht (§3.3).
   Der appkit-`APScheduler` wird als Alternative dokumentiert, aber erst bei
   einem PostgreSQL-Deployment verdrahtet.

**DoD**

- Zweiter Lauf nach genau einer neuen Mail holt genau diese eine und rechnet die
  abgeleiteten Knoten inkrementell nach.

### Phase 8 — Weitere Anbieter (validiert den Port)

**Tun**

1. `mailarc-imap` (deckt iCloud **und** Gmail-App-Passwort ab): `source/` mit
   `UIDVALIDITY`/`UIDNEXT` als Cursor, Ordner → `Label`.
2. `mailarc-m365`: MSAL + MS Graph `/messages/delta`, `$value` liefert MIME.
   **Offene Entscheidung:** delegiert (pro Nutzer) vs. app-only (Tenant, Admin
   Consent). Der opake Credential-Blob (§8.1) trägt beides.

**DoD**

- **Kein Diff** in `mailarc-sync`, `mailarc-analytics` und `mailarc-core`.
  Wenn doch, war der Port falsch geschnitten.

---

## 11. Risiken

| Risiko | Gegenmaßnahme |
| --- | --- |
| Zwei Prozesse auf einer SQLite-Datei | WAL + `busy_timeout` sind gesetzt; Worker committet pro Batch, nicht pro Mail |
| Graph über den RAM (FalkorDB ist In-Memory) | Bodies gekappt, `.eml`/Anhänge auf Platte, Vektoren optional; Größe pro 10k Mails in Phase 1 **messen** |
| Vektoren verdoppeln den Graph | 1536 × 4 Byte ≈ 6 KB/Mail → 100k Mails ≈ 600 MB. Embeddings deshalb optional und eigener Job, kein Import-Schritt |
| Embedder-Wechsel invalidiert den Index | `Message.embedding_model` am Knoten, gezielte Neuberechnung (§7.4) |
| Signatur-Erkennung schlägt fehl → Template-Müll | Fixture-Korpus mit echten Fußzeilen in Phase 1; `body_clean` ist Testgegenstand, nicht Nebenprodukt |
| Gmail-Quota (250 units/user/s) | Semaphore + Backoff; `messages.batch` erst nach Phase 4 |
| Worker im Tauri-Bundle | Braucht das vendorte `uv`/Python — dasselbe offene Problem wie beim Reflex-Backend (README „Known limitation"). Vor dem Desktop-Release fällig, nicht Teil dieser Phasen |
| `client_secret` liegt beim Nutzer | `EncryptedString`; `app_database_encryption_key` muss gesetzt sein |

---

## 12. End-to-End-Verifikation

```bash
task format && task lint && task typecheck
task test                       # Coverage ≥ 80 %
```

Nach Phase 5:

```bash
task db:upgrade                 # SQLite-Schema inkl. der neuen Tabellen
task graph:upgrade              # runic: Indizes + Constraints
PROFILES=local task run         # UI auf :8080
task sync:worker                # zweites Terminal, falls supervise_worker=false
```

1. `/mail/accounts` → Gmail-Konto anlegen, Consent durchlaufen, Import starten.
2. Fortschritt im Import-Panel; `mail_sync_jobs` zeigt `running` mit steigendem
   `messages_done`.
3. Worker mit `kill -9` abschießen, neu starten → Job wird reclaimt, keine
   Duplikate.
4. Import erneut starten → Node- und Edge-Zahlen bleiben **identisch**.
5. Analysen gegenprüfen:

```bash
redis-cli -p 6379 GRAPH.QUERY mail-archive \
  "MATCH (a:Address)<-[:SENT_TO|COPIED_TO]-(m:Message)-[:SENT_TO|COPIED_TO]->(b:Address)
   WHERE a.address < b.address
   RETURN a.address, b.address, count(m) AS together ORDER BY together DESC LIMIT 10"

redis-cli -p 6379 GRAPH.QUERY mail-archive \
  "MATCH (m:Message)-[:INSTANCE_OF]->(t:Template)
   RETURN t.occurrences, t.automation_score, t.sample_text
   ORDER BY t.automation_score DESC LIMIT 10"

redis-cli -p 6379 GRAPH.QUERY mail-archive \
  "MATCH (m:Message)-[r:ABOUT]->(t:Topic)
   RETURN t.label, r.method, count(m) ORDER BY count(m) DESC LIMIT 10"
```

6. `task graph:rebuild-derived` → dieselben Ergebnisse, `Message`-Zahl unverändert.

---

## 13. Nicht in diesem Umfang

- **GraphRAG / LLM-Entitätsextraktion** — bewusst ausgeschlossen (§3.2)
- Personenauflösung über `Address` hinaus (mehrere Adressen → ein Mensch)
- Volltext-Extraktion aus Anhängen (PDF/Office)
- Produktionsreife UI (Phase 4 ist ausdrücklich ein Dummy)
- Kalender und Kontakte der Anbieter
- Freezing des Worker-Prozesses ins Tauri-Bundle
