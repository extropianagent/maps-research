# Account protection

The rule: avoid risky methods, not powerful ones. Search depth and spending are
choices made per request. Account protection is not a choice. It applies to
every search, including an exhaustive, paid one, and wherever possible the code
enforces it, so no prompt or agent can talk its way around it.

What is being protected: the owner's Google account and Google Cloud project,
and the owner's AI subscriptions.

## The three controls

| Control | Values | Who decides |
|---|---|---|
| Depth | quick, standard, deep, exhaustive | per request |
| Spending | free (default), quality with a ceiling | the owner, per request |
| Account protection | always on | nobody; it is fixed |

## Tools

| Tool | Status |
|---|---|
| Google Places Text Search, Place Details | core |
| Places Aggregate API | core |
| Maps Experience agent tools (`maps discover`) | core, only with a recorded compatible AI host |
| Gemini Maps grounding (`maps discover --via gemini`) | core supplementary, same host rule |
| Ordinary Google Maps, opened by the owner | core supplementary; every report carries the links |
| Apify, DataForSEO, contributor lookup | conditional: refused until approved |
| Outscraper | conditional, registered but not built |
| Unofficial browser scraping through a Google account | excluded |
| CAPTCHA-solving services | excluded |
| Proxy rotation to evade blocks | excluded |
| Persistent review warehouse | excluded |
| Consumer AI subscriptions used as programmatic APIs | excluded |

## What the code enforces

| Rule | Enforcement (`maps/policy.py`) |
|---|---|
| Third-party extraction only after verification | A conditional provider refuses to run without an approval record that states its data rights, retrieval method and downstream use. A token alone is not enough. Contributor lookup needs its own approval. |
| Verify new integrations before activating them | Every provider must be registered as core or conditional. An unregistered one is refused. |
| Stop and notify on enforcement | A response that mentions suspension, a ban, abuse, unusual traffic, a CAPTCHA or a terms violation halts the whole run, exits with code 3, and blocks that provider until the owner clears it. |
| No personal Google passwords, cookies or session tokens to extraction tools | Every payload sent to an extraction service is scanned. Cookie, session, password and login fields, Google session cookies and Google sign-in tokens are refused before sending. |
| Dedicated project, restricted credentials | Only an API key is accepted for Google. A personal sign-in token in its place is refused. |
| No bypassing access controls | No proxy or CAPTCHA options are ever sent, payloads carrying them are refused, and a refused request is not retried another way. |
| No consumer AI subscriptions as APIs | The toolkit calls no AI service except Google's own Gemini API for Maps grounding, with an API key. A test fails if another is added. |
| No prohibited storage or indexing | Run data expires. Retention is capped at 30 days however it is configured, and a state folder inside the repository is refused. Embeddings for semantic search live inside the run and expire with it. Nothing trains a model. |
| Grounded Maps output only to a compatible AI | `maps discover` refuses to run until the AI host is named in `MAPS_AI_HOST` and recorded as checked. |

## What the code cannot check

These stay with the owner. `maps doctor` lists them as reminders.

- The Google Cloud project is dedicated to this toolkit, and its API key is
  restricted to the Maps APIs.
- Each AI host's data settings really do exclude training on and retention of
  conversations. The record says the owner checked; the code cannot.
- The terms of a conditional provider. The approval record holds what the owner
  verified; the code cannot read a provider's terms.

## Approving a conditional provider

Before approving, establish three things from the provider's own terms and
documentation, and write them into the record:

1. **Data rights:** what right the provider claims to the data it returns.
2. **Retrieval method:** how it gets the data, and that using it requires none
   of the owner's Google credentials.
3. **Downstream use:** what the owner may do with the results.

```bash
uv run maps policy approve apify --kind provider --owner-statement "<the owner's words>" --data-rights "<finding>" --retrieval-method "<finding>" --downstream-use "<finding>"
```

Recording an AI host:

```bash
uv run maps policy approve claude-code-cloud --kind ai_host --owner-statement "<the owner's words>" --data-handling "<what the host's settings say>"
```

Approvals are the owner's decisions. An agent runs these commands only with the
owner's own statement from the current conversation, quoted word for word, and
commits the resulting `policy/approvals.json`. `maps policy revoke SUBJECT`
withdraws one.

## When a provider signals enforcement

The command exits with code 3 and its output carries a `policy_stop` block.

1. Stop. No retry, no other route to the same data.
2. Tell the owner what the provider said, in its words.
3. Commit `policy/stops.json` so the stop survives this machine.
4. The provider stays blocked until the owner decides and says so:

```bash
uv run maps policy clear-stop google --owner-statement "<the owner's words>"
```

## Known gaps

- Enforcement detection reads the wording of error messages. An unfamiliar
  wording will be treated as an ordinary error. If a provider's message looks
  like a warning and the tool did not stop, stop anyway and tell the owner.
- Run data is kept up to 30 days so a search can resume. Google's terms permit
  caching place IDs indefinitely and coordinates for 30 days, and are stricter
  about other content. Lower `MAPS_RETENTION_DAYS` or run `maps purge --all`
  after a search to keep less.
