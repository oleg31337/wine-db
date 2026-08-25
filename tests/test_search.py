"""Search, filtering, sorting, pagination and facets."""

from __future__ import annotations

from tests.conftest import create_wine, login


def seed(api):
    wines = {}
    wines["bordeaux"] = create_wine(
        api, name="Grand Bordeaux", maker="Ch. Alpha", wine_type="red",
        country="France", region="Bordeaux", vintage=2018, grape="Merlot",
    )
    wines["chablis"] = create_wine(
        api, name="Chablis Premier", maker="Dom. Beta", wine_type="white",
        country="France", region="Burgundy", vintage=2021, grape="Chardonnay",
    )
    wines["provence"] = create_wine(
        api, name="Provence Blush", maker="Dom. Gamma", wine_type="rose",
        country="France", region="Provence", vintage=2022, grape="Grenache",
    )
    wines["prosecco"] = create_wine(
        api, name="Prosecco Extra Dry", maker="Cantina Delta", wine_type="sparkling",
        country="Italy", region="Veneto", vintage=2023, grape="Glera",
    )
    wines["rioja"] = create_wine(
        api, name="Rioja Reserva", maker="Bodega Epsilon", wine_type="red",
        country="Spain", region="Rioja", vintage=2016, grape="Tempranillo",
    )
    return wines


def names(resp):
    return {item["name"] for item in resp.json()["items"]}


def test_empty_database_returns_no_items(api, user):
    body = api.get("/api/wines").json()
    assert body["total"] == 0
    assert body["items"] == []


def test_search_by_name_substring_case_insensitive(api, user):
    seed(api)
    assert names(api.get("/api/wines?q=chablis")) == {"Chablis Premier"}
    assert names(api.get("/api/wines?q=CHABLIS")) == {"Chablis Premier"}


def test_search_matches_maker_grape_and_place(api, user):
    seed(api)
    assert names(api.get("/api/wines?q=epsilon")) == {"Rioja Reserva"}
    assert names(api.get("/api/wines?q=tempranillo")) == {"Rioja Reserva"}
    assert names(api.get("/api/wines?q=veneto")) == {"Prosecco Extra Dry"}


def test_search_special_characters_are_not_wildcards(api, user):
    seed(api)
    assert api.get("/api/wines?q=%25").json()["total"] == 0
    assert api.get("/api/wines?q=_").json()["total"] == 0


def test_filter_by_single_type(api, user):
    seed(api)
    assert names(api.get("/api/wines?wine_type=red")) == {"Grand Bordeaux", "Rioja Reserva"}


def test_filter_by_multiple_types(api, user):
    seed(api)
    got = names(api.get("/api/wines?wine_type=rose&wine_type=sparkling"))
    assert got == {"Provence Blush", "Prosecco Extra Dry"}


def test_invalid_type_filter_rejected(api, user):
    assert api.get("/api/wines?wine_type=purple").status_code == 422


def test_filter_by_country_and_region(api, user):
    seed(api)
    assert api.get("/api/wines?country=France").json()["total"] == 3
    assert api.get("/api/wines?country=france").json()["total"] == 3
    assert names(api.get("/api/wines?region=Rioja")) == {"Rioja Reserva"}


def test_combined_filters_narrow_results(api, user):
    seed(api)
    assert names(api.get("/api/wines?country=France&wine_type=red")) == {"Grand Bordeaux"}


def test_filter_by_average_rating(api, user, second_user):
    wines = seed(api)
    api.put("/api/wines/" + wines["bordeaux"]["id"] + "/rating", json={"stars": 5})
    api.put("/api/wines/" + wines["rioja"]["id"] + "/rating", json={"stars": 2})

    assert names(api.get("/api/wines?min_rating=4")) == {"Grand Bordeaux"}
    assert names(api.get("/api/wines?min_rating=1&max_rating=3")) == {"Rioja Reserva"}


def test_rating_scope_mine_vs_average(api, user, second_user):
    wines = seed(api)
    target = wines["chablis"]["id"]
    api.put("/api/wines/" + target + "/rating", json={"stars": 1})
    assert login(api, "second").status_code == 200
    api.put("/api/wines/" + target + "/rating", json={"stars": 5})

    # 'second' rated it 5; the shared average is 3.
    assert names(api.get("/api/wines?min_rating=5&rating_scope=mine")) == {"Chablis Premier"}
    assert api.get("/api/wines?min_rating=5&rating_scope=average").json()["total"] == 0


def test_unrated_by_me_filter(api, user):
    wines = seed(api)
    api.put("/api/wines/" + wines["bordeaux"]["id"] + "/rating", json={"stars": 3})
    got = names(api.get("/api/wines?unrated_by_me=true"))
    assert "Grand Bordeaux" not in got
    assert len(got) == 4


def test_rating_bounds_validated(api, user):
    assert api.get("/api/wines?min_rating=9").status_code == 422
    assert api.get("/api/wines?rating_scope=everyone").status_code == 422


def test_sorting_by_name_and_vintage(api, user):
    seed(api)
    asc = [i["name"] for i in api.get("/api/wines?sort=name&order=asc").json()["items"]]
    assert asc == sorted(asc)
    vintages = [i["vintage"] for i in api.get("/api/wines?sort=vintage&order=desc").json()["items"]]
    assert vintages == sorted(vintages, reverse=True)


def test_invalid_sort_rejected(api, user):
    assert api.get("/api/wines?sort=; DROP TABLE wines").status_code == 422
    assert api.get("/api/wines?order=sideways").status_code == 422


def test_pagination_walks_the_whole_set(api, user):
    seed(api)
    first = api.get("/api/wines?limit=2&offset=0").json()
    second = api.get("/api/wines?limit=2&offset=2").json()
    third = api.get("/api/wines?limit=2&offset=4").json()
    assert first["total"] == 5
    assert len(first["items"]) == 2 and len(second["items"]) == 2 and len(third["items"]) == 1
    ids = {i["id"] for i in first["items"] + second["items"] + third["items"]}
    assert len(ids) == 5


def test_pagination_limits_are_bounded(api, user):
    assert api.get("/api/wines?limit=0").status_code == 422
    assert api.get("/api/wines?limit=5000").status_code == 422
    assert api.get("/api/wines?offset=-1").status_code == 422


def test_facets_report_present_values(api, user):
    seed(api)
    facets = api.get("/api/wines/facets").json()
    assert facets["total"] == 5
    countries = {c["name"]: c["count"] for c in facets["countries"]}
    assert countries == {"France": 3, "Italy": 1, "Spain": 1}
    types = {t["name"]: t["count"] for t in facets["types"]}
    assert types["red"] == 2
    regions = {r["name"] for r in facets["regions"]}
    assert "Bordeaux" in regions and "Veneto" in regions


def test_search_results_carry_mini_card_fields(api, user):
    seed(api)
    item = api.get("/api/wines?q=rioja").json()["items"][0]
    for key in ("id", "name", "maker", "wine_type", "country", "region", "vintage",
                "photo_url", "average_rating", "rating_count", "my_rating", "comment_count"):
        assert key in item
