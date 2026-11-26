# Data permissions

Personal use is not an exemption from the Google Maps Platform terms. This file
records what the terms allow, how the toolkit behaves because of them, and what
is still unverified. Re-read the linked terms before changing any of this
behaviour: they change, and this file was written on 2026-11-26.

Sources read in full on that date:
[Maps Platform Terms of Service](https://cloud.google.com/maps-platform/terms) and
[Service Specific Terms](https://cloud.google.com/maps-platform/terms/maps-service-terms).
Items marked *(excerpt)* come from search excerpts of developers.google.com pages
that could not be opened from the build environment.

## What the terms say

| Topic | Rule | Source |
|---|---|---|
| Place IDs | May be cached; no time limit is set. | Service Terms §3 |
| Latitude/longitude | May be cached up to 30 consecutive days, then must be deleted. | Service Terms §15.3 |
| Everything else (names, addresses, ratings, reviews) | No caching except as expressly permitted; no copying and saving of business names, addresses or user reviews. | ToS 3.2.3(a)(iii), 3.2.3(b) |
| Machine learning | Maps content must not be used to train, test, validate or fine-tune ML/AI models. | ToS 3.2.3(c)(vii) |
| Attribution | Display the attribution Google provides; do not alter or remove it. Reviews need author attribution and the content-reporting link *(excerpt)*. | ToS 3.2.2(b) |
| Non-Google maps | Places content must not be shown with a non-Google map. | Service Terms §15.2 |
| Aggregate counts | A count may be cached 30 days. | Service Terms §14.2 |
| Agent tools (grounding) | Output must keep its Google Maps source links, may be kept 30 days for evaluation, and must not go to a model that trains on or retains its input. | Service Terms §10, grounding docs *(excerpt)* |

## How the toolkit complies

- **Run data expires.** Search results live only in the state directory
  (`MAPS_STATE_DIR`, default `~/.maps-research`), outside the repository. Every
  `maps search` deletes runs older than `MAPS_RETENTION_DAYS` (default 30).
  `maps purge --all` deletes everything now.
- **Nothing from Maps is committed.** `.state/` is git-ignored and CI never
  uploads run data.
- **Long-term memory is yours, not Google's.** `knowledge/` holds place IDs,
  Google Maps links and your own notes and judgements. Do not paste review text,
  addresses or rating tables into it.
- **Reports keep attribution.** Review output carries the author name, the
  review link and the reporting link, and place output carries the Google Maps
  link.
- **No model training.** Nothing here trains or fine-tunes a model. Semantic
  review search uses a local embedding model, so review text is not sent to
  any outside service; its vectors are cached inside the run and expire with it.
- **Agent-tools output keeps its links.** `maps discover` prints Google's
  place notes with their Google Maps links and does not store the notes.

## Open questions, to settle before relying on a feature

1. **Which AI may read the results.** The agent-tools terms forbid sending
   grounded output to a model that trains on or retains inputs. The Places API
   terms do not contain that sentence, but ToS 3.2.3(c)(vii) (no use to improve
   models) points the same way. Before using an AI host as the researcher, check
   that its data settings exclude training on your conversations and note the
   result in `knowledge/search-lessons.md`.
2. **Apify and DataForSEO.** These are scrapers of Google Maps, not licensed
   access. Google's terms prohibit scraping; the providers carry that risk under
   their own terms and you carry it under yours. They are off by default, need
   their own credentials, and DataForSEO additionally needs `--cost quality`.
   Decide deliberately whether to enable them.
3. **Contributor lookup.** `maps reviews contributors --expand` fetches the
   public review history of individual reviewers through an Apify actor. That
   is personal data about named people as well as scraping. It is off unless
   `APIFY_TOKEN` is set and the flag is given. Use it on the few contributors a
   search of your own surfaced, keep the results inside the expiring run, and
   never copy a reviewer's name or history into `knowledge/`.
4. **Review text search.** Google returns at most five reviews per place and no
   full-text review search. `maps reviews search` therefore covers only the
   reviews fetched into the current run. It cannot claim citywide completeness.
