"""Shared favorites lists."""

from __future__ import annotations

from tests.conftest import create_wine, login


def make_list(api, name="Weeknight reds", **extra):
    payload = {"name": name}
    payload.update(extra)
    resp = api.post("/api/favorites", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_favorites_require_auth(client):
    assert client.get("/api/favorites").status_code == 401


def test_create_and_list(api, user):
    made = make_list(api, description="Easy drinking")
    assert made["wine_count"] == 0
    lists = api.get("/api/favorites").json()
    assert len(lists) == 1
    assert lists[0]["name"] == "Weeknight reds"
    assert lists[0]["description"] == "Easy drinking"


def test_multiple_lists_supported(api, user):
    for name in ("Cellar picks", "To buy again", "Special occasions", "Disappointments"):
        make_list(api, name)
    assert len(api.get("/api/favorites").json()) == 4


def test_duplicate_name_conflicts(api, user):
    make_list(api)
    assert api.post("/api/favorites", json={"name": "Weeknight reds"}).status_code == 409
    assert api.post("/api/favorites", json={"name": "WEEKNIGHT REDS"}).status_code == 409


def test_name_required_and_bounded(api, user):
    assert api.post("/api/favorites", json={"name": "   "}).status_code == 422
    assert api.post("/api/favorites", json={"name": "x" * 81}).status_code == 422


def test_add_and_remove_wine(api, user):
    fav = make_list(api)
    wine = create_wine(api)
    assert api.put(f"/api/favorites/{fav['id']}/wines/{wine['id']}").status_code == 204

    listed = api.get(f"/api/favorites/{fav['id']}/wines").json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == wine["id"]
    assert api.get("/api/favorites").json()[0]["wine_count"] == 1

    assert api.delete(f"/api/favorites/{fav['id']}/wines/{wine['id']}").status_code == 204
    assert api.get(f"/api/favorites/{fav['id']}/wines").json()["total"] == 0


def test_adding_twice_is_idempotent(api, user):
    fav = make_list(api)
    wine = create_wine(api)
    api.put(f"/api/favorites/{fav['id']}/wines/{wine['id']}")
    api.put(f"/api/favorites/{fav['id']}/wines/{wine['id']}")
    assert api.get(f"/api/favorites/{fav['id']}/wines").json()["total"] == 1


def test_a_wine_can_live_in_several_lists(api, user):
    a = make_list(api, "List A")
    b = make_list(api, "List B")
    wine = create_wine(api)
    api.put(f"/api/favorites/{a['id']}/wines/{wine['id']}")
    api.put(f"/api/favorites/{b['id']}/wines/{wine['id']}")
    detail = api.get(f"/api/wines/{wine['id']}").json()
    assert set(detail["favorite_list_ids"]) == {a["id"], b["id"]}


def test_unknown_list_or_wine_is_404(api, user):
    fav = make_list(api)
    wine = create_wine(api)
    assert api.put(f"/api/favorites/{fav['id']}/wines/nope").status_code == 404
    assert api.put(f"/api/favorites/nope/wines/{wine['id']}").status_code == 404
    assert api.get("/api/favorites/nope/wines").status_code == 404


def test_lists_are_shared_between_users(api, user, second_user):
    fav = make_list(api, "Shared picks")
    wine = create_wine(api)
    api.put(f"/api/favorites/{fav['id']}/wines/{wine['id']}")

    assert login(api, "second").status_code == 200
    lists = api.get("/api/favorites").json()
    assert [entry["name"] for entry in lists] == ["Shared picks"]
    assert api.get(f"/api/favorites/{fav['id']}/wines").json()["total"] == 1
    # equal access: the second user may edit and delete it too
    assert api.patch(f"/api/favorites/{fav['id']}", json={"name": "Renamed"}).status_code == 200
    assert api.delete(f"/api/favorites/{fav['id']}").status_code == 204


def test_rename_conflict_detected(api, user):
    make_list(api, "One")
    two = make_list(api, "Two")
    assert api.patch(f"/api/favorites/{two['id']}", json={"name": "One"}).status_code == 409


def test_deleting_list_keeps_the_wines(api, user):
    fav = make_list(api)
    wine = create_wine(api)
    api.put(f"/api/favorites/{fav['id']}/wines/{wine['id']}")
    assert api.delete(f"/api/favorites/{fav['id']}").status_code == 204
    assert api.get(f"/api/wines/{wine['id']}").status_code == 200


def test_deleting_wine_removes_it_from_lists(api, user):
    fav = make_list(api)
    wine = create_wine(api)
    api.put(f"/api/favorites/{fav['id']}/wines/{wine['id']}")
    api.delete(f"/api/wines/{wine['id']}")
    assert api.get(f"/api/favorites/{fav['id']}/wines").json()["total"] == 0


def test_search_can_be_scoped_to_a_favorites_list(api, user):
    fav = make_list(api)
    keep = create_wine(api, name="In the list")
    create_wine(api, name="Not in the list")
    api.put(f"/api/favorites/{fav['id']}/wines/{keep['id']}")
    got = api.get(f"/api/wines?favorite_list_id={fav['id']}").json()
    assert [i["name"] for i in got["items"]] == ["In the list"]
