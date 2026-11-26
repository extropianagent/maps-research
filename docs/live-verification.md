# Live verification checklist

The toolkit was built and tested without network access to the real APIs: every
HTTP call in the test suite is mocked. The items below were taken from provider
documentation (partly through search excerpts) or from memory and have not been
confirmed against a live response. Confirm each on first live use, then update
this file and the code together. Tick an item only after seeing a real response.

## Google Places API (New): `maps/providers/google_places.py`

- [ ] Text Search returns at most 60 results per query and area (3 pages of 20).
      Saturation detection assumes it. Check: a broad query in a dense area
      (`maps search -q restaurant --area "…" --depth quick --max-tasks 1` with
      `max_results` 60 via `--depth standard --max-tasks 1`) returns exactly 60.
- [ ] Whether a `nextPageToken` is still offered on the third page.
- [ ] `places.viewport` is accepted in a Text Search field mask (`maps area`),
      and which SKU it bills under (assumed: Text Search Pro).
- [ ] `locationRestriction.rectangle` excludes everything outside the box.
- [ ] `minRating` still filters when the field mask is IDs-only (a free way to
      bucket by rating; see `knowledge/search-lessons.md`).
- [ ] Place Details returns at most 5 reviews, and each carries `originalText`,
      `publishTime`, `authorAttribution.uri`, `googleMapsUri`, `flagContentUri`.
- [ ] An exhausted daily quota is signalled with HTTP 429 (the search then
      stops cleanly and `--resume` continues the next day).
- [ ] Field mask to SKU mapping matches `maps/pricing.py`: compare one day of
      use against the Google Cloud billing report, SKU by SKU.

## Places Aggregate API: `maps/providers/google_aggregate.py`

- [ ] Request field names: `filter.locationFilter.customArea.polygon.coordinates`,
      `typeFilter.includedTypes`, `ratingFilter.minRating/maxRating`,
      `operatingStatus`.
- [ ] Response shape: `count` (string or number) and `placeInsights[].place`
      as `places/<id>`.
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

## Hosts

- [ ] Which AI hosts may read the results under their data settings
      (`docs/permissions.md`, open question 1).
