# Tools and skills catalog

Outside tools that were looked at, what each does, and what was decided. The
rule for this file: reuse first, but only what was audited, and only where it
adds a capability the toolkit lacks. Nothing here is approved for use by being
listed. Third-party extraction services still need the approval described in
`docs/account-protection.md`.

Audited 2026-11-26. Open-source entries were cloned and their code read without
running it. Re-audit before adopting anything: these projects change.

## Google's own resources

| Resource | What it is | Decision |
|---|---|---|
| Maps Demo Key | An API key from <https://mapsplatform.google.com/maps-demo-key>. Needs a personal Google account; no billing account, card or Cloud project. Covers the Places API and the agent tools among others, with a daily limit, under its own Maps Demo Project terms. Reported limit: 100 calls per API per day, and no reviews or photos. Not for production. Source: Google's agent-skills repository, read directly; the limit and the reviews exclusion from search excerpts. | Use for the first live verification. Move to a billed, quota-capped key for real use. |
| Maps Experience agent tools | Google's MCP endpoint for place search in plain language. Two billing tiers; the higher one applies to pages above 5 places and extended details. | In use: `maps discover`. The toolkit never requests the higher tier. |
| Gemini grounding with Google Maps | The Gemini API answering with Maps places attached. Price and free allowance are reported inconsistently. | In use, optional: `maps discover --via gemini`. Treated as paid until the price is confirmed. |
| googlemaps/agent-skills | Google's guidance skill for coding agents building Maps apps (Apache 2.0). Mostly about writing app code. | Reference. Its Demo Key steps and its advice to restrict keys are reflected in `docs/setup.md`. |
| Ask Maps | Conversational search inside the Google Maps app. No API or export found. | Manual baseline in `evals/benchmark-protocol.md`. |

## Open-source tools

| Tool | How it gets data | Findings | Decision |
|---|---|---|---|
| [cablate/mcp-google-map](https://github.com/cablate/mcp-google-map) | Official Google APIs with a key | Broad MCP server (places, routes, weather, air quality). No pagination past the first page. Puts the key in the URL on some calls; HTTP mode listens on all interfaces by default. | Reference. One idea noted below. |
| [steipete/goplaces](https://github.com/steipete/goplaces) | Official Places API (New) with a key | Clean, small, key sent in a header. Nothing this toolkit lacks. | Reference only. |
| [deanmikan/gmaps-cli](https://github.com/deanmikan/gmaps-cli) | Google's agent tools endpoint and Routes | A thin client for the same endpoint `maps discover` uses. | Reference only. A different project of the same name drives a browser and would be rejected. |
| [apify-alexey/google-maps-radar-search](https://github.com/apify-alexey/google-maps-radar-search) | Legacy Nearby Search with a key | The closest match to "grid search". Adds eight overlapping circles around a saturated point. More redundant calls than splitting a cell in four; last changed in 2022. | Not adopted. The existing subdivision is cleaner. |
| [kirvahe/restaurant-skill](https://github.com/kirvahe/restaurant-skill) | Web search and web scraping; no Maps API | Good preference design (below). Its installer adds a hook that re-downloads the skill daily, which would let remote text change an agent's instructions; it also scrapes. | Do not install. Preference design adapted. |
| Hotel Skill | | No project of that name was found. | Not found. |

## Commercial extraction services

All are conditional: refused by the toolkit until an approval is recorded.

| Service | Notes | Status |
|---|---|---|
| Apify | Place search, reviews, contributor lookup. | Built, conditional. |
| DataForSEO | Place search and reviews. | Built, conditional. |
| Outscraper | Same ground as the two above. | Registered, not built. |
| [HasData](https://github.com/HasData/hasdata-mcp) | An MCP proxy to a scraping service, including reviews and contributor reviews. | Registered, not built. |

## Named in the master plan, not audited here

Recorded so they are not rediscovered. None was read or run; the descriptions
are the plan's, taken from vendor claims and posts. None is enabled.

| Tool | What it is said to be | Status |
|---|---|---|
| [Hotelist](https://hotelist.com/) and its MCP | A curated hotel directory with its own rating method. | Outside the Maps-only scope. Only on the owner's explicit request, kept separate from Maps results. |
| Booking.com | Dates, prices, room filters. | Same: the owner checks price and availability there; nothing is imported. |
| Bing Maps | Another map corpus. | Outside scope. |
| [maps-contrib](https://github.com/adnXone/maps-contrib) | Per-contributor review export by browser automation. | Excluded: automated scraping of reviewer histories. |
| [ScrapePass](https://scrapepass.com/tools/google-maps-reviews-scraper) | Per-business review export. | Conditional at most; not registered. |
| [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) | Unofficial bulk extraction. | Excluded. |
| MapLeadFinder | A browser-extension scraper. | Excluded. |
| [Billwarden](https://billwarden.com/) | Billing-spike monitoring. | A possible extra alert. It would need access to billing data, so audit it first; it does not replace the spending gateway. |

## Ideas taken, and ideas noted

**Taken, from the Restaurant Skill:** preferences live in plain files the owner
can read; each visit is logged with the owner's own rating; after every fifth
logged visit the agent proposes at most two changes to the preferences and
makes none without the owner's yes. See `knowledge/feedback-log.md`.

**Noted, not built:**

- A visibility tracker (from cablate): search the same keyword from a grid of
  points and report how often and how high a place appears. The toolkit already
  records where each place first surfaced; a dedicated measurement would be an
  experiment for `evals/`.
- Sweeping several place types in one pass (from the radar search). `maps
  enumerate` does this by type through the Aggregate API.

## Keeping this current

Add a row when a tool is looked at, with the date and what was read. A tool
moves from "noted" to "in use" only through the provider registry in
`maps/policy.py` and an experiment in `evals/` showing it adds something.
