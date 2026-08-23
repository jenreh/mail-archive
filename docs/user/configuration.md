# Configuration

## Three layers, in this order

1. **`configuration/config.yaml`** — the base. Everything that has a value has
   one here.
2. **`configuration/config.<profile>.yaml`** — layered on top, chosen by the
   `PROFILES` environment variable.
3. **The environment** — beats both.

Settings come from `appkit_commons`, which reads the YAML through pydantic
settings. Every value marked `secret:name` is looked up through the configured
secret provider instead of being read literally.

## Profiles

```sh
PROFILES=local task run
```

| Profile | File | What it changes |
| --- | --- | --- |
| `local` | `config.local.yaml` | Development logging, ports 8080/3030, local FalkorDB |
| `prod` | `config.prod.yaml` | **Single port 8080**, local FalkorDB |
| `devcontainer` | `config.devcontainer.yaml` | Devcontainer networking |
| *(none)* | `config.yaml` alone | Production logging, ports 8080/3030 |

Profiles compose: `PROFILES=local,prod` is legal. `config.prod.yaml`
deliberately sets neither `environment` nor `logging`, so it can be layered onto
`local` without dragging production logging into a development run.

### Two traps that cost real hours

**`PROFILES` must not go in `.env`.** `appkit_commons` calls
`load_dotenv(override=True)` at import time, so anything in `.env` beats the
real process environment. A `PROFILES` line there makes `PROFILES=prod task …`
silently do nothing. It is commented out in `.env.default` for that reason.
Export it per entry point instead — the Taskfiles and the Tauri launcher do.

**One profile set per process.** The YAML reader is cached while the merge
mutates its result in place, so the first merge in a process permanently
rewrites the cached base. A second merge with different profiles inherits the
first one's values — and only for the keys the new profile does not itself set,
which produces configuration that looks half-applied. Never call `configure()`
twice with different profiles.

## Environment variables

Two forms, both work.

**Nested, through `AppConfig`** — double underscore per level:

```sh
APP__GRAPH__MODE=remote
APP__GRAPH__HOST=falkordb.internal
APP__DATABASE__ECHO=true
```

**Per component, through its own prefix** — each component's config class reads
its own:

| Prefix | Class | Covers |
| --- | --- | --- |
| `APP_GRAPH_` | `GraphConfig` | The graph server and its driver |
| `APP_ARCHIVE_` | `ArchiveConfig` | Blob store and body cap |
| `APP_MAIL_` | `MailConfig` | Parser behaviour |
| `APP_SYNC_` | `SyncConfig` | Engine and worker |
| `APP_GOOGLE_` | `GmailConfig` | Gmail endpoints and paging |
| `APP_IMAP_` | `ImapConfig` | IMAP timeouts, paging and certificate authority |
| `APP_M365_` | `M365Config` | The Entra application, Graph endpoints and paging |

## Every setting

### Graph — `app.graph` / `APP_GRAPH_`

| Setting | Default | Notes |
| --- | --- | --- |
| `mode` | `local` | `local` starts and supervises the server; `remote` only connects |
| `backend` | `falkordb` | `falkordb`, `neo4j`, `memgraph`, `arcadedb`, `age` |
| `host` | `127.0.0.1` | |
| `port` | `6379` | |
| `graph_name` | `mail-archive` | The graph FalkorDB and AGE address |
| `database` | *unset* | Bolt backends address a database; falls back to `graph_name` |
| `username` / `password` | *unset* | Bolt backends only |
| `startup_timeout` | `15.0` | Seconds to wait for a local server. Ignored when remote |
| `data_dir` | `.state/falkordb` | Ignored when remote |
| `runtime_dir` | *unset* | Where the vendored binaries are. Ignored when remote |

`mode: local` requires `backend: falkordb` and the config refuses anything
else — the vendored binaries are a `redis-server`, and nothing else fits behind
that. Everything else is something someone else operates.

Queries go through runic's OGM against its driver protocol, so the backend is a
real choice. Only the server's *lifecycle* and its Redis-level status
(`INFO`, `MODULE LIST`, `GRAPH.LIST`) are FalkorDB-specific; for other backends
the status snapshot simply leaves those fields empty.

### Archive — `app.archive` / `APP_ARCHIVE_`

| Setting | Default | Notes |
| --- | --- | --- |
| `store_dir` | `.state/mailstore` | Content-addressed store for the original bytes |
| `body_text_limit` | `65536` | Characters of body the graph node keeps — 64 KB |

Both are limits, not switches. The whole message always stays in the blob store;
`body_text_limit` only caps what gets indexed.

### Mail parsing — `app.mail` / `APP_MAIL_`

| Setting | Default | Notes |
| --- | --- | --- |
| `shingle_size` | `3` | Words per shingle for the SimHash |
| `strip_quotes` | `true` | Drop `>` lines and everything past a reply intro |
| `strip_signatures` | `true` | Drop sign-offs, `--` blocks and legal disclaimers |

Turning either off makes the cleaned body closer to the raw text — which is
exactly the failure the cleaning exists to prevent. Without it, every message
carrying the same company footer hashes alike and the template analysis returns
noise. They are switches for a debugging session, not for production.

### Sync — `app.sync` / `APP_SYNC_`

| Setting | Default | Notes |
| --- | --- | --- |
| `batch_size` | `100` | References per listing page, and the archive write batch |
| `fetch_concurrency` | `8` | Concurrent fetch streams |
| `checkpoint_every` | `200` | Messages between two checkpoints of a **full** walk |
| `incremental_interval` | `0.0` | Seconds between two sweeps for new mail; `0` is off |
| `poll_interval` | `2.0` | Seconds an idle worker waits before asking again |
| `lease_seconds` | `120.0` | How long a claim survives without a heartbeat |
| `heartbeat_interval` | `10.0` | Seconds between lease extensions |
| `worker_id` | `<pid>@<host>` | Who claims jobs; distinct per process |
| `supervise_worker` | `true` | Whether the web app starts the worker itself |

`incremental_interval` is off by default on purpose: a fresh install must not
start talking to your mailbox on its own, so the first sync is a button you
press. Set it to `900` and the worker looks for new mail every fifteen minutes,
skipping any account that is disabled, waiting for a re-consent, already
syncing, or **whose first full import has not finished**.

That last one is why turning the schedule on before pressing **Import** does not
start the archive off: a delta asks the provider what changed since a point in
time, and a mailbox nobody has walked has no such point. The schedule waits, and
says so once in the log at debug level. Press **Import**, let it run to the end,
and every sweep after that picks up where it left off.

`fetch_concurrency` limits the provider's patience, not ours — eight concurrent
conversations keep a first import moving without earning a rate limit.

`lease_seconds` must stay comfortably longer than `heartbeat_interval`, or a
merely slow worker has its job stolen while it still holds the graph session.

Set `supervise_worker: false` under Docker or systemd, where the worker is its
own unit. Leave it on for the desktop app, where there is nobody else to do it.

### Gmail — `APP_GOOGLE_`

| Setting | Default | Notes |
| --- | --- | --- |
| `api_base_url` | `https://gmail.googleapis.com/gmail/v1` | Settings, not constants, so tests can point at a local server |
| `token_uri` | `https://oauth2.googleapis.com/token` | |
| `loopback_port` | `0` | `0` lets the OS pick — a fixed port is a collision waiting for the second window |
| `consent_timeout` | `300` | Seconds the consent flow waits for the browser to come back before it gives up and frees the port |
| `request_timeout` | `30.0` | Generous, because a raw message with attachments is a large body |
| `page_size` | `100` | Gmail's own maximum is 500; a hundred keeps a cancel prompt |

Not one of these names a mailbox. Which account, whose credentials and how far
the last run got are **state in SQLite**, not configuration — a second Gmail
account must not need a second config.

### IMAP — `app.imap` / `APP_IMAP_`

| Setting | Default | Notes |
| --- | --- | --- |
| `connect_timeout` | `15.0` | Seconds for the TCP connect and the TLS handshake together. Short: a host that has not answered by then is a typo or a network that is down |
| `request_timeout` | `120.0` | Seconds for one IMAP command. Generous, because a `UID SEARCH` over a huge mailbox and a large attachment are each one unchunked answer |
| `page_size` | `200` | UIDs per listing page. `UID SEARCH` has no server-side paging, so this is the slice taken off its answer |
| `tls_ca_file` | `""` | A PEM bundle for a private certificate authority. Empty means the platform trust store |

Shorter than the others, and deliberately so: the server, the port, the username,
the app password and the folder belong to the **mailbox**, so they are credential
fields and live encrypted in the database. That is what lets one archive hold an
iCloud account and a Gmail-app-password account at the same time.

**There is no setting to turn TLS off.** An app password in the clear is the
credential itself, so port 143 with `STARTTLS` is a downgrade this adapter does
not offer.

### Microsoft 365 — `app.m365` / `APP_M365_`

Shipped **commented out** in `configuration/config.yaml`: `client_id` and
`client_secret` are `secret:` references, those are resolved while the
configuration is validated, and a key the secret provider does not hold stops the
application starting. Put the values in your `.env` first (`.env.default` carries
the placeholders), then uncomment the block.

| Setting | Default | Notes |
| --- | --- | --- |
| `client_id` | `""` | The Entra application this installation signs in as. Empty means Microsoft 365 is not set up |
| `client_secret` | unset | App-only only. A delegated sign-in is a public client and must not carry one |
| `api_base_url` | `https://graph.microsoft.com/v1.0` | Also the origin fence: a stored cursor is a whole URL, and one from anywhere else is refused |
| `authority_host` | `https://login.microsoftonline.com` | A setting only so a test can serve it |
| `default_tenant` | `common` | Accepts work, school and personal accounts. A single-organisation deployment names its own |
| `delta_folder` | `allitems` | Graph has no mailbox-wide delta; every one is scoped to a folder. `inbox` for a tenant whose mailboxes have no `AllItems` |
| `loopback_port` | `0` | `0` lets the OS pick. Entra accepts any loopback port for a public client |
| `consent_timeout` | `300.0` | Seconds the sign-in waits for the browser to come back |
| `request_timeout` | `60.0` | Longer than Gmail's: `$value` returns the whole MIME message in one body |
| `token_timeout` | `30.0` | Seconds MSAL may spend at the token endpoint |
| `page_size` | `100` | Message references per listing call; Graph's own ceiling is 1000 |
| `watermark_page_size` | `500` | Larger, because nothing consumes those pages — the drain only wants the link at the end |
| `watermark_max_pages` | `200` | A bound, not a target. Stopping early is not a failure: the mark simply lands mid-chain and the next run carries on |

`delta_folder` is the one apparent exception to "no setting names a mailbox", and
it is not one: it names a folder *inside every* mailbox, the same one for all of
them.

### Semantic — `app.semantic` / `APP_SEMANTIC_`

Off by default, and off is a complete state: without an embedder A1–A3 run in
full and only semantic search and A2's sixth signal are missing. See
[Semantic search](./semantic-search.md) for what turning it on costs and what
an assistant reading the archive can see.

| Setting | Default | Notes |
| --- | --- | --- |
| `provider` | `none` | `none`, `ollama` (local, no account) or `openai` (uploads every body it embeds) |
| `model` | `""` | Empty means the provider's own default — `nomic-embed-text` or `text-embedding-3-small` |
| `dimension` | `768` | **Must equal the vector migration's `DIMENSION`.** The only length both providers can produce |
| `base_url` | `""` | Empty means the provider's own default; a setting so tests can point at a local server |
| `api_key` | unset | OpenAI needs one; Ollama ignores it. A `SecretStr` |
| `batch_size` | `32` | Texts per HTTP call. Tuned for the local model, which chokes where a paid API does not |
| `page_size` | `500` | Messages per graph round trip — the job's unit of memory and of progress |
| `request_timeout` | `120.0` | A cold local model really is that slow |
| `max_body_chars` | `8000` | ~2 000 tokens. Past that an embedding describes the quoted thread, not the message |
| `knn_over_fetch` | `10` | FalkorDB's KNN cannot be filtered before the fact, so a search over-fetches and cuts |
| `topic_similarity_min` | `0.82` | Signal 6's gate. At 0.7 an invoice and a delivery note are neighbours in every model |
| `topic_neighbours` | `5` | How many close messages one message may name |
| `task_prefix` | `false` | Whether the Ollama adapter prefixes its input with a task instruction |

`dimension` is the one that cannot be changed in place. FalkorDB accepts a
vector of any other length, stores it and **silently declines to index it** — no
error, no log line — so the embed job reads the live index's dimension before it
writes anything and refuses a mismatch by name. Changing it needs a new graph
migration and a full re-embed.

### Database — `app.database`

```yaml
database:
  type: sqlite
  url: sqlite+aiosqlite:///.state/mail-archive.db
  encryption_key: secret:mn-db-encryption-key
  echo: false
```

The URL names an **async** driver because every application session is async.
Alembic and Reflex need the blocking one and convert it themselves by stripping
`+aiosqlite`.

Three pragmas are applied to every connection this process opens, and they are
not optional: WAL (so a reader is not blocked by a writer), `foreign_keys=ON`
(without it SQLite silently ignores `ON DELETE CASCADE`), and a 5-second busy
timeout (so a second writer waits instead of raising `database is locked`).

## Secrets

`.env` holds the local ones:

```sh
SECRET_PROVIDER=local          # or azure
mn-db-encryption-key=<a Fernet key>
```

`SECRET_PROVIDER=azure` plus `AZURE_KEY_VAULT_URL` reads them from Key Vault
instead. No credential belongs in code or in git.

`mn-db-encryption-key` encrypts `mail_credentials.secret`. Losing it does not
harm the archive, but every account has to be reconnected.

## Recipes

**Local development**

```sh
PROFILES=local task run
```

**A graph someone else runs**

```sh
PROFILES=prod APP__GRAPH__MODE=remote APP__GRAPH__HOST=falkordb.internal task run
```

**Neo4j instead of FalkorDB**

```yaml
app:
  graph:
    mode: remote          # required — only FalkorDB runs locally
    backend: neo4j
    host: neo4j.internal
    port: 7687
    database: mailarchive
    username: neo4j
    password: secret:mn-graph-password
```

**Worker as its own unit**

```yaml
app:
  sync:
    supervise_worker: false
```

```sh
PROFILES=prod uv run python -m app.worker
```
