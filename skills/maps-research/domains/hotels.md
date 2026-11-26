# Hotels and other places to stay

Read after the brief in `../SKILL.md`. This adds what is specific to lodging.

## What Maps data cannot tell you
Prices, availability and room types are not in the Places data this toolkit
reads. Say so in the report, every time. The search finds and vets places; the
owner checks price and dates elsewhere.

## Area and keywords
- Location decides most hotel choices. Search `--near` the place the owner
  needs to be, with a radius they would walk or drive, before any city-wide search.
- Query every kind of stay the area uses: hotel, guest house, serviced or
  furnished apartments, hostel, resort, and the local terms for each.

## Filters and ranking
- Ratings bunch between 3.8 and 4.5, so small gaps mean little. Rank with `best`
  and let the evidence step separate them.
- Under about 30 reviews is thin for a hotel. Keep such places only as "unverified".

## Evidence (required, with integrity)
- Hotels attract incentivised reviews. Run `maps reviews integrity RUN` on the
  shortlist.
- Word sets. Quality: "clean", "quiet", "bed", "water pressure", "air
  conditioning", "wifi", "staff". Failure: "bedbugs", "mold", "smell", "noise",
  "thin walls", "hidden fee", "deposit", "not as pictured", "construction".
- Management and renovations change a hotel quickly. Weigh the newest reviews
  most, and report a split between old and new.

## Report
Group picks by distance from the owner's anchor point. State that price and
availability were not checked.

## Preferences worth recording
Budget band, noise sensitivity, bed and room needs, chains liked or avoided,
anchor locations they return to.
