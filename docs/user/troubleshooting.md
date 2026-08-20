# Troubleshooting

## The graph server will not start

**"the vendored FalkorDB binaries are missing"**

`redis-server` and `falkordb.so` are not where the app looked. Run
`task tauri:vendor`, or point `app.graph.runtime_dir` at where they are, or set
the `MAIL_ARCHIVE_FALKORDB_DIR` environment variable — that wins over both.

**It starts and immediately dies**

Almost always a missing dylib. The FalkorDB module links OpenSSL at absolute
paths, and `dlopen` failing produces an opaque `redis-server` error rather than
a useful one. The runtime checks for `libssl.3.dylib` and `libcrypto.3.dylib` up
front for exactly that reason. If they are there and it still dies, read the
last 50 lines of server output that `FalkorDBServer` keeps — module load
failures show up there and nowhere else.

**"backend=neo4j cannot be started locally"**

`mode: local` supervises a `redis-server`, so it only works with
`backend: falkordb`. Use `mode: remote` for anything else.

**The status page says unreachable but the server is running**

Check the port. An unreachable server is a valid status, not an error — the
reader returns `reachable=false` with the reason instead of raising, so the page
can render "down" like any other state. The reason is in `error`.

## Configuration looks half-applied

Symptom: some settings follow the profile you asked for, others keep the
previous one's values.

Two causes, both covered in [configuration](configuration.md):

- `PROFILES` is set in `.env`, where it beats the real environment. Comment it
  out.
- Something called `configure()` twice with different profiles in one process.
  The YAML reader caches and merges in place, so the second merge inherits the
  first. Only one profile set is safe per process.

## Reflex refuses to start in production mode

> frontend and backend must run on the same port

`config.prod.yaml` exists to align them on 8080. Make sure `prod` is in your
`PROFILES`.

## `database is locked`

The busy timeout should absorb this — a blocked writer waits five seconds
rather than raising. Seeing it anyway means a writer held a transaction longer
than that. Usually: two workers running against the same database. Check that
`sync.supervise_worker` is not starting a second one alongside the one you
started yourself.

## `ON DELETE CASCADE` did nothing

SQLite silently ignores foreign keys unless `PRAGMA foreign_keys=ON` was set on
that connection, and it is per-connection — SQLite keeps none of it across
connects. The pragmas are installed on the SQLAlchemy `Engine` class rather than
on one engine, because three separate engines end up open against the same file
(appkit's sync and async engines, and Reflex's). A connection opened outside
that path will not have them.

## An import stopped and the job says `failed`

Read `error` on the job row. The common ones:

| Error | Meaning |
| --- | --- |
| Auth failure | The token was rejected or revoked. The account is now `auth_error`; reconnect, do not retry |
| Transient, after retries | The engine already backed off five times per slice. That is an outage. The job goes back to the queue and resumes at the checkpoint |
| `no provider registered for 'gmail'` | The account names a provider this process did not register. A configuration problem, not a mailbox problem |
| `job N names account M, which is gone` | The account was deleted while the job was queued |
| `account N has no stored credential` | Setup was never finished — the account exists but nothing opens it |

## A job has been `running` for hours with no progress

Its worker may be dead. The lease is what decides: while `lease_until` is in the
future the job is claimed, and once it stops moving the job is up for grabs
again. `reclaim_expired()` puts it back in the queue.

`worker_id` is `<pid>@<hostname>` precisely so you can tell what to kill and
where.

## Messages are missing from the graph

Check `mail_failed_messages` first. Every message the import drops leaves a row
there with a reason and a detail — skipping is allowed, silence is not. If the
message is not in that table and not in the graph, it was never listed.

## A reply is not linked to the message it replies to

Expected, if the reply was imported before its parent. The writer deliberately
does not create a placeholder node: a `Message` holding nothing but an id would
be indistinguishable from a real one and would poison every count and every
analysis walking the graph.

The loss is recoverable — the headers that would rebuild the link are still on
the message and in the blob store. A fabricated node would not be.

## `task clean` deleted my archive

It does. `task clean` removes `.state/` entirely: the SQLite database, the blob
store and the graph's data directory. `task db:upgrade` recreates the schema
and the default `admin` account, but the mail is gone.

## The worker did not start

The application logs it and carries on rather than failing to boot — the pages
that show what the archive holds work fine without a worker, and a job simply
waits in the queue until one exists. Look for "Could not start the sync worker"
in the log, and for "the sync worker exited immediately with code N", which
means the child died within its half-second startup grace — usually an import
error in the worker's own dependencies.

Run it in the foreground to see the real traceback:

```sh
task sync:worker
```
