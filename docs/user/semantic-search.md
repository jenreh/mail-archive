# Semantic search

Full-text search works on every installation and needs nothing configured: it
reads an index the first migration created and finds the words you type.

Semantic search is the other half. It finds mail *about* something — an invoice
dispute, a delivery date — even when the message never uses the word you asked
for. It needs an **embedder**: a model that turns text into a vector. That is
the one part of this application which is off by default, and this page is
about turning it on.

Both live on the search page at `/`, as the two halves of its **Mode** switch.
Semantic reads the question and nothing else, so choosing it greys out the
sender, date, attachment and account fields and says so — a form that quietly
stopped honouring what is typed in it would be a search that lies about what it
searched.

## Off is a supported state, not a broken one

`app.semantic.provider` defaults to `none`, and the archive is complete without
it. Import, co-recipients, topics, templates, threads and full-text
search all work; the two things you do not get are the search page's
**Semantic** mode — the segment is disabled, with a sentence naming what to
configure — and the sixth topic signal.

That default is deliberate. The desktop app is meant to need nothing installed,
and defaulting to a local model server would mean a mail archive that refuses to
analyse anything until you had installed one.

Everywhere semantic search is unavailable, the application says so in a sentence
naming what to change. It never answers a semantic search with an empty list —
an empty list is a valid answer to a search, and reading one would tell you your
archive holds nothing on a subject when the truth is that a setting is missing.

## The three providers

| | `ollama` | `openai` | `azure_openai` |
| --- | --- | --- | --- |
| Where the text goes | your own machine | OpenAI's servers | your Azure resource |
| Account | none | API key required | API key required |
| Default model | `nomic-embed-text` | `text-embedding-3-small` | none — name your deployment |
| Default base URL | `http://localhost:11434` | `https://api.openai.com/v1` | none — name your resource |

**Every message body you embed with `openai` or `azure_openai` is uploaded off
this machine, once.** That is worth deciding on purpose for a private mail
archive. `ollama` sends nothing anywhere: it talks to a model server on
`localhost`.

The difference between the two paid providers is who holds the endpoint.
`openai` has one address every customer shares, so it needs nothing but a key.
An Azure OpenAI resource is your own, so `azure_openai` cannot guess either
half of where to send a request and **both `base_url` and `model` have to be
set**:

- `base_url` is the resource endpoint with Azure's API version path on it —
  `https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1`.
- `model` is the **deployment name** you gave the model in Azure, which is not
  always the model's own name.

Left unset, the archive refuses to embed with a message naming the setting
rather than sending a request nowhere.

## Turning it on

### From the application

**Embedder**, under **Admin** in the rail (`/admin/embedder`), sets the five
things a person can answer — provider, model, dimension, base URL and API key —
and stores them in the archive's own database. They are laid over the
configuration file, so an installation that never opens the page behaves exactly
as it did.

Four things about that page are worth knowing before you use it:

- **The API key is write-only.** It is stored encrypted, and it is never shown
  again — not on the page, not anywhere. The form says whether a key is stored,
  and leaving the box empty on a save keeps the one that is there. Removing a
  key is its own button.
- **Changing the provider, the model or the dimension is warned about before you
  save it**, with the number of messages already embedded under the old model.
  Nothing is deleted, but a semantic search under the new embedder will not find
  those messages until the vectors are rebuilt.
- **Use the configuration file** forgets everything the page stored, key
  included, and hands the archive back to `config.yaml` and the environment.
- **Rebuild the vectors** at the bottom of the page queues the embed job and
  follows it — a progress bar in messages, a Cancel that stops it between
  batches, and the count of what is embedded refreshed when it ends. This is
  the remedy the warning above points at, so changing a model and rebuilding
  are one page rather than a page and a terminal. The button is dead while the
  provider is `none`, and a second open tab follows the running job instead of
  queueing another one.

A change reaches the pages immediately. The **import worker reads these settings
when it starts**, and the embed job runs there — so restart the application (on
the desktop the worker is its own child process) before running a job under a
new embedder. For the same reason the page warns you if the form holds unsaved
changes when you press Rebuild the vectors: the worker embeds with what is
stored, not with what is on screen.

**Rebuild the vectors needs a running worker.** On the desktop it is the
application's own child process and is always there. Under Docker or systemd it
is a unit of its own, and if none is up the job simply stays queued — the page
says so rather than spinning, and the job runs as soon as a worker starts.

### From configuration

**The settings page wins.** Once `/admin/embedder` has been saved even once,
the stored row is laid over everything below and the file and the environment
answer only for what it left unset — so exporting `app_semantic_provider` on an
installation that has used the page changes nothing, and the same error message
comes back. Use **Use the configuration file** on that page to clear the stored
row and hand the archive back to this section.

Either in `configuration/config.local.yaml`:

```yaml
app:
  semantic:
    provider: ollama
```

or as environment variables, which take the same names with an `app_semantic_`
prefix:

```sh
export app_semantic_provider=ollama
```

For OpenAI you also need a key:

```yaml
app:
  semantic:
    provider: openai
    api_key: sk-...
```

For Azure OpenAI you need a key, your resource endpoint and the name of the
deployment you made there:

```yaml
app:
  semantic:
    provider: azure_openai
    base_url: https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1
    model: my-embedding-deployment
    api_key: ...
```

The key travels as Azure's own `api-key` header rather than as a bearer token;
nothing else about the request differs from `openai`, `dimension` included.

Then compute the vectors. Nothing is embedded by the import — the importer
deliberately never writes `Message.embedding`, so that changing embedder later
costs one job and not a re-import:

```sh
task graph:embed
```

That runs the job in your terminal, without a worker. **Rebuild the vectors** on
`/admin/embedder` queues the same job for the worker instead, which is the only
one of the two a desktop installation has. Either way the job is resumable and
idempotent: it embeds only what has no vector under the current model, reports
its progress per batch, and stops between batches when you cancel it, leaving
everything it had already written in place.

## The settings

| Setting | Default | What it decides |
| --- | --- | --- |
| `provider` | `none` | `none`, `ollama`, `openai` or `azure_openai` |
| `model` | provider's own | The embedding model's name — the deployment name on Azure |
| `dimension` | `768` | Floats per vector. **Must match the graph migration** |
| `base_url` | provider's own | Where the embedding API lives — required on Azure |
| `api_key` | unset | The key; both paid providers need one, Ollama ignores it |
| `batch_size` | `32` | Texts per HTTP call |
| `page_size` | `500` | Messages per graph round trip |
| `request_timeout` | `120` | Seconds one call may take — a cold local model is slow |
| `max_body_chars` | `8000` | How much of a body is embedded (~2 000 tokens) |
| `knn_over_fetch` | `10` | Neighbours asked for per neighbour returned |
| `topic_similarity_min` | `0.82` | Signal 6's gate — below this, not the same topic |
| `topic_neighbours` | `5` | How many close messages one message may name |
| `task_prefix` | `false` | Whether to prefix Ollama's input with a task instruction |

The first five are the ones `/admin/embedder` offers; the rest are calibration
and stay in the file, because a form offering `topic_similarity_min` invites a
change to the one number keeping signal 6 out of half the archive.

`dimension` is the one that is not free to change. The graph's vector index is
migrated to a fixed length, and FalkorDB accepts a vector of any other length,
stores it and **silently declines to index it** — no error, no log line. So the
job checks the live index before it writes anything and refuses a mismatch by
name. Changing the dimension needs a new migration, not a setting.

## Changing embedder later

`Message.embedding_model` is written onto every node beside its vector, which is
what makes a change detectable — and what `/admin/embedder` counts when it warns
you before a save. Run the job again after changing `model` or `provider` — with
**Rebuild the vectors** on that same page, or `task graph:embed` — and it
re-embeds exactly the messages whose stored model is not the current one.

A search only ever ranks vectors from the model it is searching under, so a
half-finished re-embed gives you *fewer* results rather than wrong ones, and the
notice under the results says how many messages are still missing.

## Reading the notice

A semantic result may carry a line like:

> 41 of 1 204 messages have no nomic-embed-text embedding yet and cannot be
> found by a semantic search — run the embed job to include them.

That is the count of messages a job could still fix — **Rebuild the vectors** on
`/admin/embedder` is what starts one. Messages with no body text
at all — an attachment-only mail, a reply that is entirely quoted — are not in
it: there is nothing to embed, so no job will ever reach them and warning about
them forever would only teach you to ignore the warning that matters.

## What the archive lets a model do

Semantic search is also what an assistant reaches through the MCP server. It is
worth being explicit about the boundary:

- A model connected through `mail-archive-mcp` can **read the whole archive** —
  every message, sender, thread and correspondent pair. There are no per-mailbox
  permissions and no authentication of any kind.
- The transport is stdio only. The boundary is the operating-system process:
  anything on your machine that can run the installed `mail-archive-mcp` command
  can read your mail, exactly as anything that can read the archive directory
  itself can — that is `.state/` from a checkout, and the per-user directory
  under `~/Library/Application Support/` for the built app.
- Nothing a model produces is written back. Every tool is read-only, and the
  archive has no path by which a model could add, change or delete anything.
- **An installation may not have the server at all.** It is an optional extra,
  and the desktop bundle is built without it — a mail archive that serves no
  MCP has none of this surface. Running `mail-archive-mcp` there tells you which
  flag to install it with.

See [Wiring an MCP client](../developer/mcp-server.md) for how to connect one.
