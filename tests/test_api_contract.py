"""Contract tests: what the Google providers send and read, checked against Google's own API
descriptions (Discovery documents) saved under tests/fixtures.

Refresh the fixtures (then re-run the tests; a failure is a real change in Google's API):

    curl -o tests/fixtures/places_discovery_v1.json 'https://places.googleapis.com/$discovery/rest?version=v1'
    curl -o tests/fixtures/areainsights_discovery_v1.json 'https://areainsights.googleapis.com/$discovery/rest?version=v1'

Current copies were fetched 2026-11-26 (revision 20261123).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from maps.budget import Budget, Ledger
from maps.geography import BBox
from maps.models import Query
from maps.providers.google_aggregate import PlacesAggregate
from maps.providers.google_places import TIER_FIELDS, GooglePlaces

FIXTURES = Path(__file__).parent / "fixtures"
PLACES = json.loads((FIXTURES / "places_discovery_v1.json").read_text())
AREA = json.loads((FIXTURES / "areainsights_discovery_v1.json").read_text())
PS: dict[str, Any] = PLACES["schemas"]
AS: dict[str, Any] = AREA["schemas"]
BBOX = BBox(24.0, 46.0, 25.0, 47.0)


def props(schemas: dict[str, Any], name: str) -> dict[str, Any]:
    return schemas[name]["properties"]


def ref(schemas: dict[str, Any], name: str, field: str) -> str:
    prop = props(schemas, name)[field]
    return (prop.get("items") or prop)["$ref"]


def methods(doc: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(r: dict[str, Any]) -> None:
        for res in r.get("resources", {}).values():
            out.extend(res.get("methods", {}).values())
            walk(res)

    walk(doc)
    return out


def method(doc: dict[str, Any], path: str) -> dict[str, Any]:
    """The method with this path; for `v1/{+name}` the place `get` (not photo `getMedia`)."""
    (m,) = [m for m in methods(doc) if m["path"] == path and not m["id"].endswith("getMedia")]
    return m


class Capture:
    def __init__(self, payload: dict[str, Any] | None = None, pages: int = 1) -> None:
        self.requests: list[httpx.Request] = []
        self.payload = payload or {}
        self.pages = pages

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        data = dict(self.payload)
        if len(self.requests) < self.pages:
            data["nextPageToken"] = "tok"
        return httpx.Response(200, json=data)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.content]

    @property
    def masks(self) -> list[str]:
        return [r.headers["x-goog-fieldmask"] for r in self.requests]


def places(cap: Capture, tier: str = "enterprise") -> GooglePlaces:
    client = httpx.Client(transport=httpx.MockTransport(cap))
    return GooglePlaces(
        Budget(Ledger(":memory:")), api_key="K", client=client, tier=tier, sleep=lambda s: None
    )


def aggregate(cap: Capture) -> PlacesAggregate:
    client = httpx.Client(transport=httpx.MockTransport(cap))
    return PlacesAggregate(Budget(Ledger(":memory:")), api_key="K", client=client)


# -- Places: Text Search request ------------------------------------------------------------


def test_text_search_body_keys_are_request_properties():
    cap = Capture(pages=2)
    places(cap).text_search(
        Query("bakery", "ar"), bbox=BBOX, min_rating=4.0, included_type="bakery", max_results=40
    )
    assert len(cap.bodies) == 2
    assert "pageToken" in cap.bodies[1]  # the page-2 request is covered
    allowed = set(props(PS, "GoogleMapsPlacesV1SearchTextRequest"))
    for body in cap.bodies:
        assert set(body) <= allowed, set(body) - allowed
    assert {"textQuery", "languageCode", "locationRestriction", "minRating", "includedType"} <= set(
        cap.bodies[0]
    )


def test_text_search_location_restriction_matches_schemas():
    cap = Capture()
    places(cap).text_search(Query("bakery"), bbox=BBOX)
    restriction = cap.bodies[0]["locationRestriction"]
    req = "GoogleMapsPlacesV1SearchTextRequest"
    rtype = ref(PS, req, "locationRestriction")
    assert set(restriction) <= set(props(PS, rtype))
    vtype = ref(PS, rtype, "rectangle")
    rect = restriction["rectangle"]
    assert set(rect) == {"low", "high"} <= set(props(PS, vtype))
    for corner in rect.values():
        ltype = ref(PS, vtype, "low")
        assert set(corner) == {"latitude", "longitude"} <= set(props(PS, ltype))
    assert rect["low"] == {"latitude": 24.0, "longitude": 46.0}
    assert rect["high"] == {"latitude": 25.0, "longitude": 47.0}


def test_text_search_and_details_method_and_path():
    cap = Capture({"id": "abc"})
    gp = places(cap)
    gp.text_search(Query("bakery"), bbox=None)
    gp.details("abc")
    search, details = cap.requests
    ms = method(PLACES, "v1/places:searchText")
    assert ms["httpMethod"] == search.method == "POST"
    assert search.url.path == "/" + ms["path"]
    md = method(PLACES, "v1/{+name}")
    assert md["httpMethod"] == details.method == "GET"
    assert "places/abc" == details.url.path.removeprefix("/v1/")
    assert md["parameters"]["name"]["pattern"].startswith("^places/")
    assert "languageCode" in md["parameters"]


def test_details_query_params_exist():
    cap = Capture({"id": "abc"})
    places(cap).details("abc", language="ar")
    md = method(PLACES, "v1/{+name}")
    assert set(cap.requests[0].url.params) <= set(md["parameters"])


# -- Places: field masks --------------------------------------------------------------------


def check_mask(mask: str, root: str) -> None:
    place = props(PS, "GoogleMapsPlacesV1Place")
    for path in mask.split(","):
        if path == "nextPageToken":
            assert path in props(PS, "GoogleMapsPlacesV1SearchTextResponse")
        elif root == "places" and path.startswith("places."):
            assert path.removeprefix("places.") in place, path
        elif root == "":
            assert path in place, path
        else:
            raise AssertionError(f"unexpected mask path {path!r}")


@pytest.mark.parametrize("tier", sorted(TIER_FIELDS))
def test_search_and_details_masks_resolve(tier):
    cap = Capture({"id": "abc"})
    gp = places(cap, tier)
    gp.text_search(Query("bakery"), bbox=BBOX)
    gp.details("abc", tier=tier)
    search_mask, details_mask = cap.masks
    assert search_mask.startswith("nextPageToken")
    check_mask(search_mask, "places")
    assert "places." not in details_mask
    check_mask(details_mask, "")


def test_reviews_and_resolve_area_masks_resolve():
    cap = Capture({"places": [{"id": "a", "viewport": {"low": {}, "high": {}}}]})
    gp = places(cap)
    gp.reviews("abc")
    try:
        gp.resolve_area("Riyadh")
    except Exception:  # noqa: BLE001 - viewport is empty; only the request matters here
        pass
    reviews_mask, area_mask = cap.masks
    check_mask(reviews_mask, "")
    check_mask(area_mask, "places")


def test_resolve_area_body_keys_are_request_properties():
    cap = Capture({"places": []})
    try:
        places(cap).resolve_area("Riyadh")
    except Exception:  # noqa: BLE001
        pass
    assert set(cap.bodies[0]) <= set(props(PS, "GoogleMapsPlacesV1SearchTextRequest"))


# -- Places: response fields the mapping code reads -----------------------------------------


def test_response_fields_read_exist_and_map_through():
    place_fields = props(PS, "GoogleMapsPlacesV1Place")
    for name in (
        "id",
        "displayName",
        "location",
        "rating",
        "userRatingCount",
        "primaryType",
        "types",
        "formattedAddress",
        "priceLevel",
        "businessStatus",
        "websiteUri",
        "nationalPhoneNumber",
        "googleMapsUri",
        "reviews",
    ):
        assert name in place_fields, name
    text_type = ref(PS, "GoogleMapsPlacesV1Place", "displayName")
    assert {"text", "languageCode"} <= set(props(PS, text_type))
    assert {"latitude", "longitude"} <= set(props(PS, ref(PS, "GoogleMapsPlacesV1Place", "location")))
    payload = {
        "id": "abc",
        "displayName": {"text": "Bakery One"},
        "location": {"latitude": 24.5, "longitude": 46.5},
        "rating": 4.2,
        "userRatingCount": 17,
        "primaryType": "bakery",
        "types": ["bakery", "food"],
        "formattedAddress": "1 Main St",
        "priceLevel": "PRICE_LEVEL_MODERATE",
        "businessStatus": "OPERATIONAL",
        "websiteUri": "https://example.com",
        "nationalPhoneNumber": "011 111 1111",
        "googleMapsUri": "https://maps.google.com/?cid=1",
    }
    assert set(payload) <= set(place_fields)
    page = places(Capture({"places": [payload]})).text_search(Query("bakery"), bbox=None)
    (p,) = page.places
    mapped = {
        "id": p.place_id,
        "displayName": p.name,
        "latitude": p.lat,
        "longitude": p.lng,
        "rating": p.rating,
        "userRatingCount": p.review_count,
        "primaryType": p.primary_type,
        "types": p.types,
        "formattedAddress": p.address,
        "priceLevel": p.price_level,
        "businessStatus": p.business_status,
        "websiteUri": p.website,
        "nationalPhoneNumber": p.phone,
        "googleMapsUri": p.maps_url,
    }
    assert all(v not in (None, "", []) for v in mapped.values()), mapped


def test_review_fields_read_exist_and_map_through():
    review_fields = props(PS, "GoogleMapsPlacesV1Review")
    author_fields = props(PS, "GoogleMapsPlacesV1AuthorAttribution")
    text_fields = props(PS, "GoogleTypeLocalizedText")
    assert {"rating", "text", "originalText", "publishTime", "authorAttribution", "googleMapsUri"} <= set(
        review_fields
    )
    assert "flagContentUri" in review_fields
    assert {"displayName", "uri"} <= set(author_fields)
    assert {"text", "languageCode"} <= set(text_fields)
    payload = {
        "rating": 2,
        "text": {"text": "translated", "languageCode": "en"},
        "originalText": {"text": "original", "languageCode": "ar"},
        "publishTime": "2026-01-02T03:04:05Z",
        "authorAttribution": {"displayName": "Sam", "uri": "https://maps.google.com/u/1"},
        "googleMapsUri": "https://maps.google.com/r/1",
        "flagContentUri": "https://maps.google.com/flag/1",
    }
    assert set(payload) <= set(review_fields)
    (r,) = places(Capture({"reviews": [payload]})).reviews("abc")
    mapped = {
        "rating": r.rating,
        "text": r.text,
        "languageCode": r.language,
        "publishTime": r.published,
        "displayName": r.author,
        "uri": r.author_id,
        "googleMapsUri": r.url,
        "flagContentUri": r.flag_url,
    }
    assert all(v not in (None, "", []) for v in mapped.values()), mapped


def test_business_status_enum_contains_closed_permanently():
    enum = props(PS, "GoogleMapsPlacesV1Place")["businessStatus"]["enum"]
    assert "CLOSED_PERMANENTLY" in enum
    import maps.filters as filters

    assert "CLOSED_PERMANENTLY" in Path(filters.__file__).read_text()


# -- Places Aggregate -----------------------------------------------------------------------


def check_filter(f: dict[str, Any]) -> None:
    assert set(f) <= set(props(AS, "Filter")), set(f) - set(props(AS, "Filter"))
    loc = f["locationFilter"]
    assert set(loc) <= set(props(AS, "LocationFilter"))
    assert set(loc["customArea"]) <= set(props(AS, "CustomArea"))
    polygon = loc["customArea"]["polygon"]
    assert set(polygon) <= set(props(AS, "Polygon"))
    for c in polygon["coordinates"]:
        assert set(c) == {"latitude", "longitude"} <= set(props(AS, "LatLng"))
    assert set(f["ratingFilter"]) <= set(props(AS, "RatingFilter"))
    assert set(f["typeFilter"]) <= set(props(AS, "TypeFilter"))
    statuses = props(AS, "Filter")["operatingStatus"]["items"]["enum"]
    assert f["operatingStatus"] and set(f["operatingStatus"]) <= set(statuses)


@pytest.mark.parametrize("operational_only", [True, False])
def test_aggregate_request_body_matches_schemas(operational_only):
    cap = Capture({"count": "3", "placeInsights": [{"place": "places/a"}]})
    agg = aggregate(cap)
    kw = dict(types=["bakery"], min_rating=1.5, max_rating=3.5, operational_only=operational_only)
    agg.count(BBOX, **kw)
    agg.place_ids(BBOX, **kw)
    insight_enum = props(AS, "ComputeInsightsRequest")["insights"]["items"]["enum"]
    assert [b["insights"] for b in cap.bodies] == [["INSIGHT_COUNT"], ["INSIGHT_PLACES"]]
    for body in cap.bodies:
        assert set(body) <= set(props(AS, "ComputeInsightsRequest"))
        assert set(body["insights"]) <= set(insight_enum)
        check_filter(body["filter"])


def test_aggregate_response_keys_and_path():
    resp = props(AS, "ComputeInsightsResponse")
    assert {"count", "placeInsights"} <= set(resp)
    assert "place" in props(AS, "PlaceInsight")
    assert ref(AS, "ComputeInsightsResponse", "placeInsights") == "PlaceInsight"
    cap = Capture({"count": "3", "placeInsights": [{"place": "places/a"}]})
    agg = aggregate(cap)
    assert agg.count(BBOX, types=["bakery"]) == 3
    assert agg.place_ids(BBOX, types=["bakery"]) == ["a"]
    m = method(AREA, "v1:computeInsights")
    for req in cap.requests:
        assert req.method == m["httpMethod"] == "POST"
        assert req.url.path == "/" + m["path"]
        assert req.url.host == "areainsights.googleapis.com"


def test_rating_filter_documented_range():
    desc = props(AS, "RatingFilter")["minRating"]["description"]
    assert "between 1.0 and 5.0" in desc
