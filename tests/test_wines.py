"""Wine CRUD, validation, search, ratings, comments and photos."""

from __future__ import annotations

from tests.conftest import create_wine, login, make_image, wine_payload


# --------------------------------------------------------------- auth wall
def test_all_wine_endpoints_require_auth(client):
    assert client.get("/api/wines").status_code == 401
    assert client.get("/api/wines/facets").status_code == 401
    assert client.get("/api/wines/anything").status_code == 401


# --------------------------------------------------------------- create/read
def test_create_and_fetch_wine(api, user):
    wine = create_wine(api)
    assert wine["name"] == "Château Test"
    assert wine["wine_type"] == "red"
    assert wine["rating_count"] == 0
    assert wine["my_rating"] is None
    assert wine["photo_url"] is None

    fetched = api.get("/api/wines/" + wine["id"]).json()
    assert fetched["grape"] == "Cabernet Sauvignon"
    assert fetched["comments"] == []
    assert fetched["favorite_list_ids"] == []


def test_minimal_wine_only_needs_a_name(api, user):
    resp = api.post("/api/wines", json={"name": "Just A Name"})
    assert resp.status_code == 201
    assert resp.json()["wine_type"] == "other"


def test_missing_wine_is_404(api, user):
    assert api.get("/api/wines/does-not-exist").status_code == 404


# --------------------------------------------------------------- validation
def test_name_is_required(api, user):
    assert api.post("/api/wines", json={"wine_type": "red"}).status_code == 422
    assert api.post("/api/wines", json={"name": "   "}).status_code == 422


def test_invalid_wine_type_rejected(api, user):
    assert api.post("/api/wines", json=wine_payload(wine_type="orange")).status_code == 422


def test_all_five_types_accepted(api, user):
    for wine_type in ("red", "white", "rose", "sparkling", "other"):
        resp = api.post("/api/wines", json=wine_payload(name="T-" + wine_type, wine_type=wine_type))
        assert resp.status_code == 201, resp.text
        assert resp.json()["wine_type"] == wine_type


def test_out_of_range_numbers_rejected(api, user):
    assert api.post("/api/wines", json=wine_payload(alcohol_pct=140)).status_code == 422
    assert api.post("/api/wines", json=wine_payload(alcohol_pct=-1)).status_code == 422
    assert api.post("/api/wines", json=wine_payload(sugar_g_l=900)).status_code == 422
    assert api.post("/api/wines", json=wine_payload(vintage=1500)).status_code == 422
    assert api.post("/api/wines", json=wine_payload(vintage=3000)).status_code == 422


def test_gauges_must_be_zero_to_five(api, user):
    for field in ("acidity", "sweetness", "body", "mouthfeel", "wood"):
        assert api.post("/api/wines", json=wine_payload(**{field: 6})).status_code == 422
        assert api.post("/api/wines", json=wine_payload(**{field: -1})).status_code == 422
        resp = api.post("/api/wines", json=wine_payload(name="ok " + field, **{field: 0}))
        assert resp.status_code == 201


def test_barcode_must_be_digits(api, user):
    assert api.post("/api/wines", json=wine_payload(barcode="abc123")).status_code == 422
    assert api.post("/api/wines", json=wine_payload(barcode="3760040370019")).status_code == 201


def test_unknown_field_is_rejected(api, user):
    assert api.post("/api/wines", json=wine_payload(sneaky="x")).status_code == 422


def test_over_long_name_rejected(api, user):
    assert api.post("/api/wines", json=wine_payload(name="x" * 201)).status_code == 422


def test_validation_error_shape_is_field_level(api, user):
    body = api.post("/api/wines", json=wine_payload(alcohol_pct=999)).json()
    assert body["detail"] == "Validation failed"
    assert any(e["field"] == "alcohol_pct" for e in body["errors"])


# --------------------------------------------------------------- update/delete
def test_every_field_is_editable(api, user):
    wine = create_wine(api)
    changes = {
        "name": "Renamed Cuvée",
        "maker": "New Maker",
        "wine_type": "white",
        "country": "Italy",
        "region": "Piedmont",
        "vintage": 2021,
        "grape": "Nebbiolo",
        "sugar_g_l": 4.5,
        "alcohol_pct": 12.0,
        "aromas": "rose petal, tar",
        "barcode": "1234567890123",
        "acidity": 5,
        "sweetness": 0,
        "body": 2,
        "mouthfeel": 4,
        "wood": 1,
    }
    resp = api.patch("/api/wines/" + wine["id"], json=changes)
    assert resp.status_code == 200
    got = resp.json()
    for key, value in changes.items():
        assert got[key] == value, key


def test_partial_update_leaves_other_fields_alone(api, user):
    wine = create_wine(api)
    updated = api.patch("/api/wines/" + wine["id"], json={"region": "Médoc"}).json()
    assert updated["region"] == "Médoc"
    assert updated["grape"] == "Cabernet Sauvignon"


def test_field_can_be_cleared_to_null(api, user):
    wine = create_wine(api)
    updated = api.patch("/api/wines/" + wine["id"], json={"maker": None, "wood": None}).json()
    assert updated["maker"] is None
    assert updated["wood"] is None


def test_any_user_may_edit_and_delete_any_wine(api, user, second_user):
    wine = create_wine(api)  # created by 'taster'
    assert login(api, "second").status_code == 200
    assert api.patch("/api/wines/" + wine["id"], json={"name": "Edited by second"}).status_code == 200
    assert api.delete("/api/wines/" + wine["id"]).status_code == 204


def test_delete_removes_the_wine(api, user):
    wine = create_wine(api)
    assert api.delete("/api/wines/" + wine["id"]).status_code == 204
    assert api.get("/api/wines/" + wine["id"]).status_code == 404


# --------------------------------------------------------------- ratings
def test_rating_lifecycle(api, user):
    wine = create_wine(api)
    rated = api.put("/api/wines/" + wine["id"] + "/rating", json={"stars": 4}).json()
    assert rated["my_rating"] == 4
    assert rated["average_rating"] == 4.0
    assert rated["rating_count"] == 1

    changed = api.put("/api/wines/" + wine["id"] + "/rating", json={"stars": 2}).json()
    assert changed["my_rating"] == 2
    assert changed["rating_count"] == 1

    assert api.delete("/api/wines/" + wine["id"] + "/rating").status_code == 204
    assert api.get("/api/wines/" + wine["id"]).json()["my_rating"] is None


def test_rating_must_be_one_to_five(api, user):
    wine = create_wine(api)
    for bad in (0, 6, -3, 99):
        assert api.put("/api/wines/" + wine["id"] + "/rating", json={"stars": bad}).status_code == 422


def test_average_combines_users_but_my_rating_is_personal(api, user, second_user):
    wine = create_wine(api)
    api.put("/api/wines/" + wine["id"] + "/rating", json={"stars": 5})
    assert login(api, "second").status_code == 200
    api.put("/api/wines/" + wine["id"] + "/rating", json={"stars": 3})

    detail = api.get("/api/wines/" + wine["id"]).json()
    assert detail["average_rating"] == 4.0
    assert detail["rating_count"] == 2
    assert detail["my_rating"] == 3
    assert {r["username"] for r in detail["ratings"]} == {"taster", "second"}


# --------------------------------------------------------------- comments
def test_comment_of_1000_plus_characters_is_accepted(api, user):
    wine = create_wine(api)
    body = "a" * 1500
    resp = api.post("/api/wines/" + wine["id"] + "/comments", json={"body": body})
    assert resp.status_code == 201
    assert len(resp.json()["body"]) == 1500


def test_comment_upper_bound_enforced(api, user):
    wine = create_wine(api)
    assert api.post("/api/wines/" + wine["id"] + "/comments", json={"body": "a" * 4001}).status_code == 422


def test_blank_comment_rejected(api, user):
    wine = create_wine(api)
    assert api.post("/api/wines/" + wine["id"] + "/comments", json={"body": "   "}).status_code == 422


def test_comment_appears_in_detail_with_author(api, user):
    wine = create_wine(api)
    api.post("/api/wines/" + wine["id"] + "/comments", json={"body": "Lovely with lamb."})
    detail = api.get("/api/wines/" + wine["id"]).json()
    assert detail["comment_count"] == 1
    assert detail["comments"][0]["username"] == "taster"
    assert detail["comments"][0]["body"] == "Lovely with lamb."


def test_only_the_author_can_edit_or_delete_a_comment(api, user, second_user):
    wine = create_wine(api)
    comment = api.post("/api/wines/" + wine["id"] + "/comments", json={"body": "Mine."}).json()

    assert login(api, "second").status_code == 200
    path = "/api/wines/" + wine["id"] + "/comments/" + comment["id"]
    assert api.patch(path, json={"body": "Hijacked"}).status_code == 403
    assert api.delete(path).status_code == 403

    assert login(api, "taster").status_code == 200
    assert api.patch(path, json={"body": "Edited by me"}).status_code == 200
    assert api.delete(path).status_code == 204


def test_deleting_wine_cascades_comments_and_ratings(api, user):
    wine = create_wine(api)
    api.put("/api/wines/" + wine["id"] + "/rating", json={"stars": 5})
    api.post("/api/wines/" + wine["id"] + "/comments", json={"body": "Note"})
    assert api.delete("/api/wines/" + wine["id"]).status_code == 204
    fresh = create_wine(api, name="Another")
    assert fresh["rating_count"] == 0
