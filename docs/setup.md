# Setup

You can try the toolkit with no setup beyond section 1, using `--provider sim`. Real searches need a Google Maps API key (section 2). Nothing here has been run against the live Google APIs yet; see `docs/live-verification.md`.

## 1. What you need

Install `uv`, a Python tool runner: https://docs.astral.sh/uv/

Then, in the repo folder, install the toolkit:

```bash
uv sync
```

Check what is configured. This makes no API call and prints no secret values:

```bash
uv run maps doctor
```

## 2. Google Maps API key

Required for real searches.

**Fastest start, no billing: a Maps Demo Key.** Open
<https://mapsplatform.google.com/maps-demo-key>, sign in with a personal Google
account, accept the Maps Demo Project terms and generate the key. It needs no
billing account or card. It has a daily limit (reported as 100 calls per API),
reportedly returns no reviews, and is for trying things out, not for regular
use. It is enough to confirm the toolkit works against the real service. Use
the steps below when you want reviews, the Aggregate API or more volume.

1. Create a Google Cloud project at https://console.cloud.google.com and attach a billing account. Billing is required even to use the free monthly allowances.
2. Enable two APIs: "Places API (New)" and "Places Aggregate API".
3. Create an API key and restrict it to those two APIs.
4. Set daily quota caps on both APIs: APIs & Services, then the API, then Quotas. Google's quotas are per day, not per month, so a daily cap bounds the month at 30 times its value. This is the hard stop behind the toolkit's own guard, and it matters most in cloud sessions, where the toolkit's usage ledger is lost with each container (section 5). Pick one of two positions. The free allowances are from `maps/pricing.py` (checked 2026-11-26, still unconfirmed), and whether the console offers a separate cap per request type is also unconfirmed.

   | Position | Text Search requests/day | Place Details requests/day | Aggregate requests/day | Worst case if the guard fails every day |
   |---|---|---|---|---|
   | Strict zero | 33 | 33 | 160 | $0: 30 days of the cap stays inside 1,000, 1,000 and 5,000 free |
   | Room for a deep search in one day | 300 | 100 | 160 | about $350 a month: (9,000 − 1,000) × $0.035 + (3,000 − 1,000) × $0.025 |

   Under strict zero a deep search takes several days: when Google refuses a request the search stops, and `maps search --resume RUN` continues the next day without repeating anything. Start with strict zero.
5. Provide the key as the environment variable `GOOGLE_MAPS_API_KEY`.

Never paste the key into a chat or into a file in this repo. `.env*` files are unreadable to agents here on purpose.

## 3. Where to run it

### Claude Code on the web, or the Claude app (cloud session on this repo)

- The session needs network access to `places.googleapis.com` and `areainsights.googleapis.com`.
- Store the key as a secret on the cloud environment. Either ask Claude to request the credential, which opens a secure form, or add it in the environment's settings. It is sent in the `X-Goog-Api-Key` header for those two hosts.
- In this setup the sandbox never sees the real key. Set `GOOGLE_MAPS_API_KEY` to a placeholder such as `set-by-proxy`, so the tool sees a configured key, and the network proxy substitutes the real one. To be confirmed on first live run.

### Any other agent host with a shell (Grok's cloud computer, Codex cloud, a VPS)

Clone the repo, install `uv`, and set the variables in that host's secret store. Run `uv run maps doctor`, then point the agent at `AGENTS.md`. These hosts have not been tested with this toolkit.

Before using any host, check that its data settings do not train on or retain your conversations: see `docs/permissions.md`, open question 1.

## 4. Optional providers

Both are conditional. Credentials alone do not enable them: each also needs a recorded approval, see `docs/account-protection.md`.

- Apify: `APIFY_TOKEN`. It has a monthly free credit that the guard tracks.
- DataForSEO: `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD`. It always costs money, so it only runs with `--cost quality --max-usd N`.

Both are scrapers of Google Maps, not licensed Google access. Read `docs/permissions.md` and decide deliberately before enabling either.

## 5. Optional features

- Semantic review search (`maps reviews search --mode hybrid`): install with

```bash
uv sync --extra semantic
```

  The first use downloads a multilingual model of about 220 MB from
  huggingface.co, so that host must be reachable. The model then runs locally.
- Meaning-based discovery (`maps discover`): uses the same Google key. Enable
  "Maps Grounding Lite API" (Google's agent tools) on the same project. It also
  needs the AI host recorded as compatible: set `MAPS_AI_HOST` to a name for
  the host and record the check, see `docs/account-protection.md`.
- Gemini Maps grounding (`maps discover --via gemini`): optional, needs
  `GEMINI_API_KEY` from Google AI Studio and `--cost quality`, because its
  price is unconfirmed.
- MCP server: see `docs/mcp.md`.

`uv run maps doctor` shows which of these are installed.

## 6. State and retention

Search results are stored outside the repo, in `MAPS_STATE_DIR` (default `~/.maps-research`). Runs older than `MAPS_RETENTION_DAYS` (default 30) are deleted at the start of every search. To delete everything now:

```bash
uv run maps purge --all
```

In an ephemeral cloud container the state directory disappears with the container. Runs and the usage ledger then reset, so the ledger undercounts across sessions. This is another reason the Google quota cap in section 2 matters.

## 7. First live run checklist

1. Check the key is seen:

```bash
uv run maps doctor
```

2. Resolve your city to a search box (one Text Search Pro call):

```bash
uv run maps area "<your city>"
```

3. Run one cheap search:

```bash
uv run maps search -q bakery --area "<your city>" --depth quick
```

4. See what it used:

```bash
uv run maps budget
```

5. Work through `docs/live-verification.md` and tick each item only after seeing a real response.
