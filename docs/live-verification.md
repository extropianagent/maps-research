# Live verification checklist

No request with an API key has been made yet: every billable call in the test
suite is mocked. The unticked items below come from provider documentation
(partly through search excerpts) and have not been seen in a live response.
Confirm each on first live use, then update this file and the code together.
Tick an item only after seeing a real response.

## Confirmed without a key, 2026-11-26

Google publishes machine-readable descriptions of these APIs, and its agent
tools endpoint answers the MCP handshake and tool listing without a key. Both
were fetched and are saved under `tests/fixtures/`; `tests/test_api_contract.py`
checks the providers against them.

- [x] Text Search and Place Details: method paths, every request field the
      provider sends, every field in every field mask, and every Place and
      Review field it reads, including `viewport`, `originalText`,
      `authorAttribution.uri`, `googleMapsUri` and `flagContentUri`.
- [x] At most 20 results per page; at most 5 reviews per place, sorted by
      relevance; `minRating` works in 0.5 steps; `locationRestriction` excludes
      results outside the box (as documented).
- [x] Places Aggregate: `computeInsights` path, every request field, the
      `insights` and `operatingStatus` values, `count` as a string, and
      `placeInsights[].place`. Rating bounds must be between 1.0 and 5.0.
- [x] Maps Experience agent tools: endpoint, handshake, tool names, and the
      input and output schema of `search_places`.

What a description cannot show is behaviour and billing. Those stay open below.

## Google Places API (New): `maps/providers/google_places.py`

- [ ] Text Search stops at 60 results per query and area (3 pages of 20).
      Saturation detection assumes it. Check with a broad query in a dense area.
- [ ] Whether a `nextPageToken` is still offered on the third page.
- [ ] `places.viewport` in a Text Search field mask bills as Text Search Pro
      (`maps area`).
- [ ] `minRating` still filters when the field mask is IDs-only (a free way to
      bucket by rating; see `knowledge/search-lessons.md`).
- [ ] An exhausted daily quota is signalled with HTTP 429 (the search then
      stops cleanly and `--resume` continues the next day).
- [ ] Field mask to SKU mapping matches `maps/pricing.py`: compare one day of
      use against the Google Cloud billing report, SKU by SKU.

## Places Aggregate API: `maps/providers/google_aggregate.py`

- [ ] `INSIGHT_PLACES` is returned only when the count is 100 or less, and what
      the API does above that (error, or count only).
- [ ] Free allowance and price (assumed 5,000 requests/month, then $10 per 1,000).

## Prices and free allowances: `maps/pricing.py`

- [ ] Every Google SKU's free monthly allowance and list price, against
      <https://developers.google.com/maps/billing-and-pricing/pricing>.
      Only the Aggregate price and the Geocoding allowance were seen in Google's
      own text; the rest came from third-party summaries.
- [ ] Apify actor prices per place and per review, and the $5 monthly credit.
- [ ] DataForSEO task prices.

## Apify: `maps/providers/apify.py` (optional, off until `APIFY_TOKEN` is set)

- [ ] Actor input names: `customGeolocation` as a GeoJSON polygon, `maxReviews`,
      `reviewsSort`, `placeIds`.
- [ ] `run-sync-get-dataset-items` returns HTTP 201 with the item array.
- [ ] Review item keys (`stars`, `publishedAtDate`, `reviewerId`, `reviewerUrl`,
      `reviewerNumberOfReviews`, `responseFromOwnerText`, `originalLanguage`).

## DataForSEO: `maps/providers/dataforseo.py` (optional, needs `--cost quality`)

- [ ] `location_coordinate` format `"lat,lng,zoom"` and the zoom derived from
      the box size.
- [ ] `depth` semantics and the item keys of `maps_search` results.
- [ ] Reviews task flow: task id field, readiness status codes, review item keys.

## Maps Experience agent tools: `maps/providers/google_agent_tools.py`

- [ ] A search with a real key returns `structuredContent` shaped as the
      listed output schema.
- [ ] It is free at the default page size, and its quota. Page sizes above 5
      and extended details are billed at a higher tier and are never requested.

## Semantic review search: `maps/semantic.py`

- [ ] The default model downloads and loads with `uv sync --extra semantic`.
- [ ] It matches Arabic reviews for English queries well enough to be useful.
      If not, try another model with `--model` and log it in
      `evals/experiments.md`.

## Contributor lookup: `ApifyProvider.contributor_reviews`

- [ ] The default actor exists, accepts `startUrls` + `maxItems`, and what it
      costs (one source says a monthly rental).
- [ ] Output field names for the place ID, place name, the contributor's own
      star rating and the review text.
- [ ] How many reviews per contributor it returns (one source says about 10).

## MCP server: `maps/mcp_server.py`

- [ ] A real client (Claude) connects over HTTP with the bearer token.
- [ ] The Docker image builds and runs; the Cloud Run deploy in `docs/mcp.md`
      works.

## Hosts

- [ ] Which AI hosts may read the results under their data settings
      (`docs/permissions.md`, open question 1).
