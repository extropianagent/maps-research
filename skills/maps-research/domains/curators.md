# Curators: find the people, then find the places

Read after the brief in `../SKILL.md`. Use it when the owner wants people whose
judgement to follow, or wants new places through curators they already trust.
A good public list from one discerning person often beats a hundred searches.

Two jobs. Do the second whenever `knowledge/curators.md` has trusted entries
for the city and domain; do the first when it has none or the yield was weak.

## What the owner gives you

Any one of: a shared Google Maps list link, a few place links, the names of
5 to 20 favourites, or only a city and a domain. Two or three places they found
overrated help, and are optional. Ask for nothing more.

## Job 1: find curators

Run two lanes on a first search.

**List and community lane (always).** Search the ordinary web for Maps lists
people chose to share. At most ten searches. Patterns that work, in the local
language and script as well:

```text
site:localguidesconnect.com "<city>" "my list"
site:localguidesconnect.com "<city>" "maps.app.goo.gl"
"<city>" "<speciality>" "Google Maps" list
"<city>" "my favorite" "google.com/maps/placelists"
"<city>" "<speciality>" "google.com/maps/d/"
"<المدينة>" "قائمة مطاعم" "قوقل ماب"
```

The last pattern finds custom maps people published with Google My Maps. Those
can hold imported outside data, so treat one as a lead until its places are
checked on Maps. Prefer a personal collection with a stated reason over a
generic "top 10", and the original list over a copy of it.
With no web search tool, give the owner the queries.

**Seed lane (when the owner has favourites).** For a few distinctive
favourites, the owner opens the place in Google Maps and looks for specific,
discerning reviews and the reviewer's public lists. You say which places and
what to look for. Obscure favourites tell more than famous ones.

**Opening a list or profile is the owner's step**, in Google Maps, signed in as
themselves. Never fetch Maps pages, and never ask for the owner's session. Hand
over at most five links, each with the one question to answer on opening it.

**Judge three things separately**, in words, with the evidence and its size:

| Question | What shows it |
|---|---|
| Credible? | Specific first-hand observations, comparisons, criticism where due |
| Same taste as the owner? | Agreement and disagreement on the owner's distinctive favourites and dislikes, with reasons |
| Finds new things? | Places the ordinary search did not surface, and later the owner's own visits |

Agreement on why matters more than agreement on a place: someone who loves the
owner's favourite bakery for its prices is not a match for an owner who loves
its crust. No combined score. Unknown is not negative. Review count, Local Guide level,
nationality, language and friendship are none of the three. Keep types apart:
a taste match, a domain specialist, an explorer and a local curator are useful
for different requests.

**Report for a curator search:** three to eight leads. For each: the public
link, where it was shared, city and domain, roughly how many places and how
recently kept up if that is visible, whether it is the author's own, one
specific reason it looks relevant, what was actually seen and what was not, and
the next step. If only a
forum profile exists, call it a lead, not a Maps profile.

## Job 2: places through trusted curators

1. The owner opens a trusted list and shares the place names or links from it
   that fit the request, or shares the list link for them to read from.
2. Resolve each place on Maps so the owner's exact filters apply:
   `maps search -q "<place name>" --near LAT,LNG --depth quick`.
3. Drop places already in `knowledge/preferences.md` or
   `knowledge/feedback-log.md`.
4. Run the evidence step of the master skill on what remains.
5. Run an ordinary search for the same request as an independent comparison,
   and say what the curator route added that the search did not. For FIND and
   HIRE the trusted collection is a starting point, never the whole search.

**Report:** the master template, with one addition per pick: which curator's
list surfaced it. Several copies of one list are one recommendation. A neutral
or negative review by a curator is not a recommendation.

## What stays off

Matching reviewers across places (`maps reviews contributors`) and fetching a
reviewer's history (`--expand`) are refused until the owner records an approval
(`docs/account-protection.md`). The manual route above needs neither. No lists
of dozens of accounts, no tracking of a person, no notes about a curator beyond
their public link and the owner's own judgement.

## Record

Add or update the curator in `knowledge/curators.md`. When the owner visits a
place a curator led to, log it in `knowledge/feedback-log.md` with the curator
in "Suggested by": that is how discovery ability gets judged.
