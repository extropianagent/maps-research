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

1. Create a Google Cloud project at https://console.cloud.google.com and attach a billing account. Billing is required even to use the free monthly allowances.
2. Enable two APIs: "Places API (New)" and "Places Aggregate API".
3. Create an API key and restrict it to those two APIs.
4. Set daily quota caps on both APIs: APIs & Services, then the API, then Quotas. This is the hard stop behind the toolkit's own guard. Start with caps that fit inside the monthly free allowances in `maps/pricing.py` (prices checked 2026-11-26, still unconfirmed):
   - Text Search Enterprise (the default tier): 1,000 free per month. At the guard's 90%, that is 900, or 30 per day. Cap at 30 per day.
   - Place Details Enterprise + Atmosphere (review fetching): 1,000 free per month. Same arithmetic, 30 per day.
   - Places Aggregate (`maps census`): 5,000 free per month. 90% is 4,500, or 150 per day. Cap at 150 per day.
   - If you use `--tier pro` for Text Search: 5,000 free, so 150 per day.
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

Both are off until their credentials are set.

- Apify: `APIFY_TOKEN`. It has a monthly free credit that the guard tracks.
- DataForSEO: `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD`. It always costs money, so it only runs with `--cost quality --max-usd N`.

Both are scrapers of Google Maps, not licensed Google access. Read `docs/permissions.md` and decide deliberately before enabling either.

## 5. State and retention

Search results are stored outside the repo, in `MAPS_STATE_DIR` (default `~/.maps-research`). Runs older than `MAPS_RETENTION_DAYS` (default 30) are deleted at the start of every search. To delete everything now:

```bash
uv run maps purge --all
```

In an ephemeral cloud container the state directory disappears with the container. Runs and the usage ledger then reset, so the ledger undercounts across sessions. This is another reason the Google quota cap in section 2 matters.

## 6. First live run checklist

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
