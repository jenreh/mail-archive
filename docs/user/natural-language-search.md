# Natural-language search

Full-text search finds the words you type. Semantic search finds the mail your
words are *about*. Both take words.

This one takes a question:

> Suche alle Nachrichten von meiner Hausverwaltung Santak

and answers it by **filling in the search form**. The sender picker ends up
holding `jb@hvsantak.de`, and the ordinary search runs against that. You can see
what it understood, and you can correct it.

It is the third half of the **Mode** switch on the search page at `/`, and it
is off until you configure a model.

## What it does with your question

One request to a model, and then arithmetic.

The model is sent your sentence, today's date, and a description of the form.
It sends back a reading: who the mail is from, roughly when, whether it carries
files, what the message itself is about. Every name in that reading is still
the words you used — the model has not been shown a single address.

The archive then does the matching itself. "meiner Hausverwaltung Santak"
becomes `jb@hvsantak.de` because that address signs itself *Hausverwaltung* and
sits at `hvsantak.de`; "Rechnungen" becomes the PDF filter because that is what
the file picker calls those files here. Then the form is filled in and the
search runs — the same search the two other modes run.

### Nothing about your archive is sent anywhere

This is the part worth deciding on purpose, and it is the opposite trade from
the one [semantic search](semantic-search.md) makes.

Semantic search uploads every message body once, to `openai` or
`azure_openai`, and gets meaning for it. Natural-language search uploads **the
sentence you typed and nothing else**. Not an address, not a mailbox name, not
a subject, not a body. The list of people you correspond with never leaves the
machine, because the matching happens here rather than there.

That is why it works this way round. Handing the model a list of candidate
addresses would have been simpler and would have meant uploading a list of
everyone you know.

With `ollama` nothing leaves the machine at all, question included.

## What it cannot do

**It does not answer the question.** "Wann habe ich die letzte Aufladung für
mein Telekom Handy gemacht" gives you the messages, newest first — not a date
in a sentence. The archive never writes a summary of your mail, and it never
sends a message body to a model.

**Newest-first and words are exclusive.** A question asking for the *last* one —
*wann*, *letzte*, *neueste* — is answered by narrowing to the sender and
ordering by date, because that is the only ordering the archive has. Doing that
means dropping the topic words, and it says so:

> Sorted newest first, so the words 'Einladung' were not used — clear the
> sender to search for them instead.

If the question named nothing but words, there is nothing to narrow by, so the
results stay in relevance order and the notice says that instead.

**A name it cannot place becomes a word.** Ask for mail from somebody this
archive has never held an address for and it will not quietly show you
everything. The name goes into the text search and you are told:

> No archived address or mailbox matches 'Frau Schmidt', so I searched for the
> words instead.

**A reading can be wrong**, and that is survivable rather than fixed. It lands
in the fields, the fields stay editable, and searching again with a correction
costs one click. A mode that ran a hidden query would have to be right.

## Turning it on

`app.llm.provider` defaults to `none`, and the archive is complete without it —
exactly as it is without an embedder. The **Natural** segment is offered greyed
out, with a sentence naming what to set.

There are two ways to set it, and the page wins over the file.

| | `ollama` | `openai` | `azure_openai` |
| --- | --- | --- | --- |
| Where the question goes | your own machine | OpenAI's servers | your Azure resource |
| Account | none | API key required | API key required |
| Default model | `qwen2.5:7b-instruct` | `gpt-4o-mini` | none — name your deployment |
| Default base URL | `http://localhost:11434` | `https://api.openai.com/v1` | none — name your resource |

### From the application

**Models**, under **Admin** in the rail (`/admin/embedder`), holds both models —
the embedder in the section above and this one below it. It sets the three
things a person can answer for the interpreter — provider, model and base URL —
plus the API key, and stores them in the archive's own database, laid over the
configuration file.

Three things about it are worth knowing:

- **The API key is write-only.** Stored encrypted and never shown again. The
  form says whether one is stored, leaving the box empty on a save keeps it,
  and removing one is its own button.
- **There is no warning before a save and nothing to rebuild.** Changing this
  model costs the next question. That is the whole difference from the embedder
  section above it, which strands every stored vector when its model changes.
- **Use the configuration file** forgets everything the interpreter section
  stored, key included, and hands that half back to `config.yaml` and the
  environment. It does not touch the embedder's settings.

A change reaches the search page immediately — no restart, because nothing
about a question is read by the worker.

### From configuration

**The settings page wins.** Once the interpreter section has been saved even
once, the stored row is laid over everything below, and the file and the
environment answer only for what it left unset. Use **Use the configuration
file** to clear the stored row and hand this half back to the file.

In `configuration/config.local.yaml`:

```yaml
app:
  llm:
    provider: ollama
```

or as environment variables, which take the same names with an `app_llm_`
prefix:

```sh
export app_llm_provider=ollama
```

For OpenAI you also need a key:

```yaml
app:
  llm:
    provider: openai
    api_key: sk-...
```

For Azure OpenAI you need a key, your resource's **v1** endpoint and the name
of the deployment you made there — none of the three can be guessed, and each
is refused by name if it is missing:

```yaml
app:
  llm:
    provider: azure_openai
    base_url: https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1
    model: my-chat-deployment
    api_key: ...
```

Only the v1 endpoint works. The older Azure surface puts the deployment in the
URL path and an API version in a query string, and this client sends neither —
pointed at one, it 404s in a way that reads like a missing deployment.

**This is a separate setting from `app.semantic`,** and deliberately so. They
are different models doing different jobs with different privacy properties.
You can run either without the other.

### Which model

Anything that follows instructions and can be held to a schema. The job is to
fill in a form, so an obedient small model beats a clever large one.

The two defaults are chosen for that: `qwen2.5:7b-instruct` runs on a laptop
and honours Ollama's structured output, and `gpt-4o-mini` is the cheapest
OpenAI model that supports schema-constrained answers. A model too old for
those is refused with a message naming `app_llm_model` — it is not quietly
downgraded to an unconstrained request, because that would keep working while
no longer honouring the schema.

## The settings

| Setting | Default | What it decides |
| --- | --- | --- |
| `provider` | `none` | `none`, `ollama`, `openai` or `azure_openai` |
| `model` | provider's own | The model's name — the deployment name on Azure |
| `base_url` | provider's own | Where the chat API lives — required on Azure |
| `api_key` | unset | The key; both paid providers need one, Ollama ignores it |
| `request_timeout` | `60` | Seconds one reading may take — somebody is waiting |
| `max_output_tokens` | `512` | A cut-off answer is truncated JSON, not a filter |
| `temperature` | `0.0` | The same question should give the same filter |
| `max_question_chars` | `500` | How much of a question reaches the model |
| `max_resolved_addresses` | `5` | How many addresses one name may stand for |

The last two are guards rather than knobs. `max_question_chars` is what stops a
pasted mail thread from becoming the prompt. `max_resolved_addresses` is what
stops a common surname from filling the **From** picker with thirty addresses —
past that the name counts as unmatched, and you are told so.

`temperature` can be set to nothing at all (`temperature:` with no value) for a
reasoning model, which accepts only its own and rejects any other.

## When it says it cannot

Every one of these is a sentence rather than an empty list, for the reason the
[semantic search page](semantic-search.md) gives at length: an empty list is a
valid answer to a search, and reading one tells you your archive holds nothing
on a subject when the truth is something else.

- **No model configured** — the segment is dead and names the setting.
- **The model refused the credentials** — your key. Not shown as a fault,
  because it is a setting you can change.
- **The model did not answer** — it is not running, or not reachable at
  `base_url`. Nothing is retried behind the button; you are told once.
- **The model did not answer with a filter** — it sent prose, or ran out of
  room. What it actually said is quoted, so you can tell a refusal from a model
  that is simply the wrong one for this.

Nothing is searched in any of those cases. An empty filter is not "nothing
matched" — it is *show me everything* — so a swallowed failure here would
answer a narrow question with your entire archive and look like it had worked.
