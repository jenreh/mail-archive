# Connecting a mailbox

An account is a mailbox this archive imports from. It has a provider, an
address, a credential that opens it, and a status.

![Connecting a mailbox](../diagrams/account-setup.svg)

## Which mailboxes work today

| Provider | What you need | Delta imports |
| --- | --- | --- |
| **Folder of `.eml` files** | A directory path. Anything you exported from Thunderbird, `mbox` split into files, a maildir. | No — every run re-lists |
| **Gmail** | An OAuth client of your own, once per installation. Then just your address. | Yes |
| **IMAP** (iCloud, Gmail app passwords, any mail host) | A server, a port, a username and an app password. | Yes |
| **Microsoft 365** | An Entra app registration, once per installation. Then a sign-in, or a tenant and a mailbox. | Yes |

All four appear in the account form in that order, and the form itself is
generated from what the provider *declares* it needs — no hand-written fields —
so the boxes you see are the boxes that provider actually uses.

"Delta imports" is the difference between a scheduled run that asks only what
changed and one that walks the whole mailbox again. It costs nothing to have; a
folder of files simply has no way to offer it.

## A folder of `.eml` files

This is a real provider, not a test double. The credential *is* the directory
path:

1. Put the `.eml` files in one directory.
2. Add an account, pick **Folder of .eml files**, and give it that path.
3. Connect. A path that is not there fails the same way a rejected password
   would — as an authentication error, with the reason.

Files are listed in sorted order and paged, so an import over them exercises
the same cursor logic a real provider does.

## Gmail

### You bring your own OAuth client

This application ships **no** client secret. The quota, the consent screen and
the audit trail belong to whoever runs it — you. That means a Google Cloud
project of your own:

1. Open the [Google Cloud console](https://console.cloud.google.com) and create
   a project (or pick one).
2. **APIs & Services → Library** → enable the **Gmail API**.
3. **APIs & Services → OAuth consent screen** → *External*, unless you have a
   Workspace tenant. Add yourself as a test user.
4. **Credentials → Create credentials → OAuth client ID** → application type
   **Desktop app**.
5. Copy the **client ID** and **client secret**.

You do not register a redirect URI. Google accepts any loopback port for a
desktop client, which is exactly what the consent flow uses.

### The one scope it asks for

```text
https://www.googleapis.com/auth/gmail.readonly
```

That is the whole list. An archive only ever reads, and asking for more would
put a capability on the consent screen that no code here can use.

### What the consent round trip does

Pressing **Connect** on a Gmail account:

1. Starts a throwaway HTTP server on `localhost`, on a port the OS picks. It
   waits at most `app.google.consent_timeout` seconds (default 300) and is
   gone afterwards whatever happened.
2. Opens your browser at Google's consent screen, preselecting the address you
   typed (`login_hint`). You sign in as yourself — this application never sees
   your password, which is the entire point of OAuth.
3. Google redirects back to that loopback port with a one-time code.
4. The code is exchanged for an access token **and a refresh token**
   (`access_type=offline`, `prompt=consent`). Google's consent page lets you
   untick the Gmail permission; a grant without it is refused on the spot
   rather than stored.
5. The account is asked whose mailbox it actually is, and the answer has to
   match the address you typed. If you consented as a different account — easy
   with several Google sessions in one browser — the grant is discarded, the
   account goes to `auth_error`, and the message names both addresses.

The refresh token is what lets an import run unattended later. Without one, an
access token dies within the hour and the account can never sync again on its
own.

::: warning While the Google project is in "Testing"
Google shows a *"Google hasn't verified this app"* page first. If pressing
**Continue** there ends on *"An error occurred"* (`/info/unknownerror`),
switch the page's language once with the dropdown at the bottom left and press
Continue again. The first render of that page is a known Google defect; the
consent itself is fine. Grants made in Testing status expire after seven days
and then ask for a reconnect.
:::

## IMAP

The only provider that needs **nothing set up on the installation**. There is no
client to register and no consent screen: an app password is complete the moment
you type it, so pressing Connect goes straight to your mail server.

Five boxes, two of them optional:

| Field | Example | Notes |
| --- | --- | --- |
| IMAP server | `imap.mail.me.com` | iCloud. Gmail is `imap.gmail.com` |
| Port | `993` | Leave empty for 993 |
| Username | `you@icloud.com` | Must match the account's email address — see below |
| App-specific password | | Not your account password |
| Folder | `INBOX` | Leave empty for `INBOX` |

**It is always TLS, on port 993.** There is no switch to turn that off and none
to use `STARTTLS` on port 143 — an app password sent in the clear on a shared
network *is* the credential. If you type 143, the connection is refused rather
than quietly downgraded.

**Use an app-specific password, not your real one.** iCloud refuses the Apple ID
password outright ([appleid.apple.com](https://appleid.apple.com) → Sign-In and
Security → App-Specific Passwords). Gmail requires one as soon as two-factor
authentication is on. Either way, an app password can be revoked on its own
without touching the rest of your account, which is exactly what you want a mail
archive holding.

### The whole account is imported

Every folder your server offers is archived, and there is nothing to pick. Each
message is tagged with the folder it came from, whole — a message in
`Reisen/Rechnungen` gets the tag `Reisen/Rechnungen`, the same way a nested
Gmail label is stored.

**Spam and deleted mail are the exception and are never imported.** The archive
uses the server's own marking for those two folders where it offers one (the
`\Junk` and `\Trash` flags), and falls back to matching the usual names —
`Junk`, `Spam`, `Bulk Mail`, `Trash`, `Deleted Messages`, `Deleted Items` —
where it does not. A folder *you* made and nested somewhere, such as
`Kunden/Junk`, is kept: only the server's own spam and trash folders are
dropped. Drafts and Sent are kept too — a draft is something you wrote, and Sent
is half of every conversation.

The folder list shown on the account is everything the server has, spam and
trash included. That describes your mailbox; it is not a claim that all of it is
archived.

::: tip Gmail over IMAP costs more than it should
Gmail's per-label folders are *views*: a message labelled `Reisen` exists in
`[Gmail]/All Mail` and in `Reisen`, with a different UID in each, and IMAP
offers no way to say they are one message. Archiving a Gmail account this way
therefore downloads each message once per label it carries. Your archive is
unaffected — the message is recognised by its Message-ID and stays a single
entry collecting several tags — but the transfer is larger than it needs to be.
Connect Gmail as a **Gmail** account instead where you can; it has a real
incremental sync and stores no password.
:::

::: warning If your mail host's usernames are not email addresses
After you press Connect the account is asked whose mailbox it is, and the answer
has to match the address on the account. IMAP has no command for "who am I", so
the only answer it can give is the username you typed. A host that issues `jens`
rather than `jens@example.com` will therefore refuse a correctly filled form —
and it discards the stored password on the way out. Use the address form of your
username if the server accepts both.
:::

## Microsoft 365

Like Gmail, this one needs a registration of your own — the app registration, its
consent screen and its Graph quota belong to whoever runs the archive.

### Register the application once

1. Open the [Microsoft Entra admin center](https://entra.microsoft.com) →
   **App registrations → New registration**.
2. Under **Authentication → Add a platform**, pick **Mobile and desktop
   applications** and add the redirect URI `http://localhost`. Entra accepts any
   loopback *port* for that, which is what the sign-in uses; nothing else has to
   be registered.
3. Under **API permissions**, add the Microsoft Graph **delegated** permissions
   `Mail.Read` and `User.Read`. `Mail.Read` reads the mailbox; `User.Read` is the
   least-privilege way to ask which account actually signed in.
4. Copy the **Application (client) ID**.

Then put it where the archive can find it. `app.m365` ships **commented out** in
`configuration/config.yaml`, because a `secret:` reference to a key you do not
have stops the application from starting at all. So: add the values to your
`.env` (`.env.default` has the placeholder lines), then uncomment the block.

```yaml
  m365:
    client_id: secret:mn-m365-client-id
    client_secret: secret:mn-m365-client-secret
```

### Two ways to sign in

The account form asks for a **sign-in mode**. Leave it empty unless you know you
want the other one.

**Delegated** (empty box) is the ordinary case: you sign in as yourself in your
own browser, Microsoft redirects back to a throwaway loopback port, and the
archive stores a refresh token. Work, school and personal accounts all work,
because the tenant defaults to `common`. Nobody's administrator is involved.

**App-only** (`app-only`) is for a shared or departmental mailbox nobody signs in
to. There is no browser step at all — permission was granted once in the tenant,
by an administrator, before the mailbox was ever added. It needs three more
things:

- the **Directory (tenant) ID**, filled into the form. Not `common`: a
  client-credentials token is issued *by* one tenant.
- the **Mailbox** address to read, filled into the form.
- a **client secret** on the registration, in `mn-m365-client-secret`, plus the
  Graph **`Mail.Read` application permission** with admin consent. A delegated
  sign-in must *not* have a secret — a desktop installation leaves it as the
  placeholder.

### Where the incremental import looks

Microsoft Graph has no mailbox-wide "what changed" feed; every one of them is
scoped to a folder. The archive uses `allitems`, the search folder that spans the
whole mailbox, which is why scheduled runs see everything. Some older hybrid and
single-tenant configurations do not have it and answer `Default folder AllItems
not found` on the first delta — such a deployment sets `app.m365.delta_folder` to
`inbox`, and accepts that mail filed elsewhere by a server-side rule is picked up
by the next full import instead.

## How the credential is stored

`mail_credentials.secret` is a single encrypted column, and its contents are
deliberately structureless: each provider serialises its own model into it —
Gmail a refresh token, IMAP a host, a username and a password,
Microsoft 365 either a refresh token or a tenant and a mailbox. That is why
adding a provider needs no schema migration.

None of them stores the OAuth client or the Entra client secret: those belong to
the installation and are configured once, so rotating one does not mean editing
every account.

The column is encrypted at rest with the Fernet key from `mn-db-encryption-key`.
Lose that key and every stored credential becomes unreadable; the archive itself
survives, but every account has to be reconnected.

## Account status

| Status | What it means | What to do |
| --- | --- | --- |
| `idle` | Connected, nothing running | Start an import |
| `syncing` | A job is walking the mailbox | Watch, or cancel |
| `auth_error` | Credentials rejected or revoked | Reconnect — retrying will not help |
| `error` | Something else went wrong | Read `last_error` |

`auth_error` is the one the UI acts on differently: it offers a reconnect
rather than a retry, because no amount of retrying fixes a revoked token.

`enabled` is your switch; `status` is the machine's report. Disabling an account
keeps whatever status its last run left behind.

## Next

[Importing mail](importing-mail.md).
