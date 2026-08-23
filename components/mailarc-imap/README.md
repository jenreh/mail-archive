# mailarc-imap

Any IMAP mailbox, behind the one seam this design allows to be abstract —
iCloud (`imap.mail.me.com:993`) and Gmail with an app password
(`imap.gmail.com:993`) being the two it was built for.

The package implements `mailarc-core`'s mail source port and nothing else:
folder listing, UID paging, and a `BODY.PEEK[]` fetch that hands the domain the
same RFC 5322 bytes every other provider hands it. Protocol shapes stop at that
boundary — no `UIDVALIDITY`, no modified-UTF-7 folder name and no IMAP flag
reaches the graph unmapped.

The package is `source/`, named after the capability rather than the protocol,
so that `mailarc_google.source` and `mailarc_m365.source` look identical.

## Layout

| Module | What lives there |
| --- | --- |
| `model.py` | IMAP's own shapes — the port, the two well-known hosts, what `EXAMINE` reports — and `IMAP_DESCRIPTOR`, where the one-folder-per-account decision is argued |
| `config.py` | `ImapConfig`: two timeouts, a page size, a certificate authority. Never an account, and never a host |
| `credentials.py` | `ImapCredentials`: what fills `mail_credentials.secret`, and the parsing that accepts the account form's all-strings JSON without quoting a password back |
| `client.py` | The blocking IMAP conversation — one connection, one selected folder, one lock — and the only place a socket or a `NO` becomes one of the four errors |
| `mapping.py` | IMAP's numbers turned into domain value objects. The cursor and the message id are minted and read here and nowhere else |
| `source.py` | `ImapSource`: the six port methods, made of the rest |

## The three decisions worth knowing before reading the code

**One folder per account.** An IMAP UID identifies a message inside one folder
and one `UIDVALIDITY`, and nothing else — the same message in `INBOX` and in
`Archive` has two unrelated UIDs and IMAP will not say they are one message. So
the folder is a credential field, a second folder is a second account, and Gmail
users point it at `[Gmail]/All Mail`, the folder Google maintains for exactly
this. Walking every folder instead would archive a Gmail mailbox once per label.

**The cursor is `UIDVALIDITY:next-UID`,** in both kinds, minted and read in
`mapping.py`. A stored `UIDVALIDITY` that no longer matches the folder's means
the server renumbered the mailbox, which is `MailCursorExpired` from a delta and
a restart from the first UID for a full walk — the engine re-raises that error
from a full walk, so raising it there would be a loop. The `provider_message_id`
carries the folder and the generation as well as the UID, because the archive's
ledger keys on it and a bare UID would let a renumbered folder look already
archived.

**One socket, eight streams.** `ImportEngine` runs `fetch_concurrency` fetch
streams against one source object. IMAP has one connection with one command in
flight and a folder selected on it, so `ImapClient` serialises every command
behind an `asyncio.Lock` and runs each one in `asyncio.to_thread`. Without the
lock the streams interleave and hand the archive the wrong message's bytes;
`tests/test_imap_source.py::TestOneSocketAndEightStreams` fails if the lock is
removed.

## Rules

- Depends on `mailarc-core` alone, plus `imapclient`. **Not `aioimaplib`** — it
  is GPL-3.0 and this project is MIT with a distributed desktop bundle.
- **No `mailarc-sync`.** The engine drives this adapter through the port; it
  never learns its name. `app/composition.py` does the registering.
- **No `mailarc-google`.** The loopback server and the OAuth module over there
  are not shared code; a component may not import a sibling. Anything this
  adapter needs of that kind is written here.
- **No Reflex, no `appkit` UI package.** The account form is generated from
  `credential_fields`, so this provider cost no UI change.
- **No `runic.rag`.**
- **Only the four errors.** Every method raises from `mailarc_core.mail.errors`
  and nothing else — an adapter that lets an `imaplib` or socket error through
  has not decided whether the engine should retry. Two of them do not look like
  protocol failures and are the ones to watch: `imapclient` decodes folder names
  from modified UTF-7, so a name that is not valid modified UTF-7 raises
  `UnicodeDecodeError` — a `ValueError` — out of `list_folders`; and
  `IMAPClient.login` wraps *every* error the command raises into a `LoginError`,
  so a socket that dies mid-login arrives claiming the password was refused.
  Both are handled in `client.py` and both have a test.
- **Always `BODY.PEEK[]`.** `RFC822` and `BODY[]` fetch the same bytes and set
  `\Seen`. Turning a stranger's unread mail read is a visible, irreversible edit
  made by a program that was asked only to copy.
- **Always TLS.** There is no setting that turns it off: an app password in the
  clear is the credential itself. `ImapConfig.tls_ca_file` points at a private
  certificate authority for a self-hosted server.

`tests/test_imap_package.py` enforces the import bans for now;
`components/mailarc-core/tests/test_isolation.py` is where they belong, and it
cannot see this package until the hand-written tuples in that file name it.

## Tests

`uv run pytest components/mailarc-imap/tests`

No test talks to a real IMAP server. `tests/imap_server.py` is a minimal
IMAP4rev1 server on a loopback socket that speaks only the commands this adapter
issues — including RFC 3501 §9's range semantics, where `UID SEARCH UID 5:*`
answers with UID 3 on a mailbox whose highest UID is 3, which is the quirk the
client's own filter exists for. `tests/tls.py` mints the throwaway certificate
it serves, so the adapter's TLS path is the path the tests take.
