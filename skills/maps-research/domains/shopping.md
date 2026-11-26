# Shopping: specialised shops and particular products

Read after the brief in `../SKILL.md`. This adds what is specific to shops.

## Keywords
- The product is the query, more than the shop: brand names, model names, the
  material, the local word for the thing. Shops that stock it are often listed
  only as "store".
- For "interesting independent shops", query themes, one search per theme
  (stationery, ceramics, vintage, records), not one broad "shops" query.
- A shop's rating says nothing about one item. Stock, exact specification and
  price are checked with the shop, not inferred from Maps.

## Filters and ranking
- `explore` for browsing requests, `find` when the owner needs a specific item.
- Malls and department stores flood the results:
  `--exclude-type shopping_mall --exclude-type department_store`.
- Chains: drop them by name with `--name-regex` only when the owner asked for
  independents.

## Evidence
- Stock is the question. Word sets. Quality: the product name, "in stock",
  "original", "authentic", "warranty", "knowledgeable". Failure: "fake",
  "counterfeit", "out of stock", "refused", "no return", "overpriced".
- A product mentioned only inside reviews is the hard case: review search covers
  only the reviews fetched into this run. Ask Google's side too, which sees more:
  `maps discover "shop that stocks <product> in <city>" --into RUN`.
- Opening hours decide a shopping trip: `maps details RUN --top N`.

## Report
Say for each pick whether a review actually mentions the item, or whether the
shop is only a likely stockist. Suggest phoning before travelling when it is
the second.

## Preferences worth recording
Brands, independents or chains, price comfort, how far they will travel.
