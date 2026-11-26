"""SKU table: the single place where free allowances and list prices live.

Values are list prices in USD, last checked against provider documentation on
the date in PRICES_CHECKED. They go stale: re-check before trusting a dollar
figure, and keep provider-side quota caps as the real stop (docs/setup.md).

A "unit" is one billable request unless the SKU says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICES_CHECKED = "2026-11-26"


@dataclass(frozen=True)
class Sku:
    provider: str
    free_units: float  # free units per month for this SKU (0 = none)
    usd_per_unit: float  # list price per unit beyond the free allowance
    note: str = ""


# Monthly free credit per provider, applied to billable usage after per-SKU free units.
PROVIDER_FREE_USD: dict[str, float] = {
    "apify": 5.0,
}

SKUS: dict[str, Sku] = {
    # --- Google Places API (New). The field mask of a request selects the SKU. ---
    "google.text_search.ids": Sku("google", float("inf"), 0.0, "Text Search Essentials (IDs Only)"),
    "google.text_search.pro": Sku("google", 5_000, 0.032, "Text Search Pro"),
    "google.text_search.enterprise": Sku("google", 1_000, 0.035, "Text Search Enterprise"),
    "google.text_search.atmosphere": Sku("google", 1_000, 0.040, "Text Search Enterprise + Atmosphere"),
    "google.details.ids": Sku("google", float("inf"), 0.0, "Place Details Essentials (IDs Only)"),
    "google.details.essentials": Sku("google", 10_000, 0.005, "Place Details Essentials"),
    "google.details.pro": Sku("google", 5_000, 0.017, "Place Details Pro"),
    "google.details.enterprise": Sku("google", 1_000, 0.020, "Place Details Enterprise"),
    "google.details.atmosphere": Sku("google", 1_000, 0.025, "Place Details Enterprise + Atmosphere"),
    # --- Google Places Aggregate API ---
    "google.aggregate": Sku("google", 5_000, 0.010, "Places Aggregate computeInsights"),
    "google.agent_tools.search": Sku(
        "google",
        float("inf"),
        0.0,
        "Maps Experience agent tools search_places (free while Experimental; unconfirmed)",
    ),
    "google.gemini_maps_grounding": Sku(
        "google", 0, 0.025, "Gemini API request grounded with Google Maps (price unconfirmed)"
    ),
    # --- Apify: unit = one scraped place / one scraped review ---
    "apify.place": Sku("apify", 0, 0.004, "Google Maps scraper, per place"),
    "apify.review": Sku("apify", 0, 0.0005, "Google Maps reviews scraper, per review"),
    "apify.contributor": Sku("apify", 0, 0.001, "Contributor reviews scraper, per result (unconfirmed)"),
    # --- DataForSEO: unit = one task ---
    "dataforseo.maps_search": Sku("dataforseo", 0, 0.002, "Google Maps SERP, live, per task"),
    "dataforseo.reviews": Sku("dataforseo", 0, 0.00075, "Google reviews, per 10 reviews"),
    # --- Offline simulator (sim.py): free, used by tests, evals and dry runs ---
    "sim.search": Sku("sim", float("inf"), 0.0, "Simulated search"),
    "sim.reviews": Sku("sim", float("inf"), 0.0, "Simulated reviews"),
}
