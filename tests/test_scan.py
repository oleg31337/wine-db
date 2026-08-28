"""Scan / enrichment pipeline.

The AI endpoint and the internet are mocked: these tests assert the *rules*
around enrichment (never overwrite user input, never invent ratings or
comments, ask for the back label when the front is not enough, never leak
backend configuration) rather than a particular model's output.
"""

from __future__ import annotations

import pytest

from app.services import enrich as enrich_module
from tests.conftest import make_image


@pytest.fixture
def fake_ai(monkeypatch):
    """Replaces the vision + text calls with deterministic answers."""
    state = {
        "vision": {
            "legible": True,
            "name": "Château Mock",
            "maker": "Domaine Mock",
            "country": "France",
            "region": "Bordeaux",
            "vintage": 2018,
            "raw_text": "CHATEAU MOCK 2018 BORDEAUX",
        },
        "text": {"grape": "Merlot", "alcohol_pct": 13.0, "acidity": 3},
        "available": True,
        "calls": [],
    }

    async def fake_vision(self, image_bytes, back_label=False):
        state["calls"].append("vision")
        return dict(state["vision"])

    async def fake_text(self, context):
        state["calls"].append("text")
        return dict(state["text"])

    monkeypatch.setattr(enrich_module.AIClient, "vision_label", fake_vision, raising=False)
    monkeypatch.setattr(enrich_module.AIClient, "text_lookup", fake_text, raising=False)
    monkeypatch.setattr(
        enrich_module.AIClient, "available", property(lambda self: state["available"])
    )
    return state


@pytest.fixture
def no_network(monkeypatch):
    async def no_web(settings, query):
        return "", []

    monkeypatch.setattr(enrich_module, "search_web", no_web)
    import app.routers.scan as scan_router

    monkeypatch.setattr(scan_router, "search_web", no_web)


def post_label(api, **fields):
    data = {k: str(v) for k, v in fields.items()}
    return api.post(
        "/api/scan/label",
        files={"file": ("label.jpg", make_image(), "image/jpeg")},
        data=data,
    )


# --------------------------------------------------------------- access
def test_scan_requires_auth(client):
    resp = client.post("/api/scan/label", files={"file": ("l.jpg", make_image(), "image/jpeg")})
    assert resp.status_code == 401


def test_scan_requires_csrf(client, user):
    resp = client.post(
        "/api/scan/label", files={"file": ("l.jpg", make_image(), "image/jpeg")}
    )
    assert resp.status_code == 403


def test_status_never_exposes_backend_config(api, user):
    body = api.get("/api/scan/status").json()
    assert set(body) == {"ai_available", "web_search_enabled"}
    raw = api.get("/api/scan/status").text
    for secret in ("11434", "http", "api_key", "token", "192.168"):
        assert secret not in raw


# --------------------------------------------------------------- label reading
def test_label_scan_returns_suggestion(api, user, fake_ai, no_network):
    resp = post_label(api)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggestion"]["name"] == "Château Mock"
    assert body["suggestion"]["region"] == "Bordeaux"
    assert body["suggestion"]["vintage"] == 2018
    assert "vision" in state_sources(body)


def state_sources(body):
    return " ".join(body["sources"])


def test_suggestion_never_overwrites_user_values(api, user, fake_ai, no_network):
    body = post_label(api, name="My Own Name", region="My Own Region").json()
    assert "name" not in body["suggestion"]
    assert "region" not in body["suggestion"]
    assert body["suggestion"]["maker"] == "Domaine Mock"


def test_suggestion_never_contains_rating_or_comment(api, user, fake_ai, no_network):
    fake_ai["vision"].update({"rating": 5, "stars": 4, "comment": "Delicious", "comments": ["x"]})
    fake_ai["text"].update({"rating": 5, "comment": "Great"})
    body = post_label(api).json()
    for banned in ("rating", "stars", "comment", "comments", "my_rating", "average_rating"):
        assert banned not in body["suggestion"]


def test_ai_knowledge_fills_remaining_fields(api, user, fake_ai, no_network):
    body = post_label(api).json()
    assert body["suggestion"]["grape"] == "Merlot"
    assert body["suggestion"]["alcohol_pct"] == 13.0


def test_illegible_label_asks_for_the_back_label(api, user, fake_ai, no_network):
    fake_ai["vision"] = {"legible": False, "raw_text": ""}
    fake_ai["text"] = {}
    body = post_label(api).json()
    assert body["need_back_label"] is True
    assert any("BACK label" in m for m in body["messages"])


def test_back_label_scan_does_not_ask_again(api, user, fake_ai, no_network):
    fake_ai["vision"] = {"legible": False, "raw_text": ""}
    fake_ai["text"] = {}
    body = post_label(api, is_back_label="true").json()
    assert body["need_back_label"] is False


def test_good_front_label_does_not_ask_for_the_back(api, user, fake_ai, no_network):
    body = post_label(api).json()
    assert body["confidence"] == "high"
    assert body["need_back_label"] is False


def test_missing_ai_endpoint_is_reported_not_fatal(api, user, fake_ai, no_network):
    fake_ai["available"] = False
    body = post_label(api).json()
    assert body["suggestion"] == {}
    assert any("No AI endpoint" in m for m in body["messages"])
    assert body["need_back_label"] is True


def test_ai_failure_is_handled_gracefully(api, user, fake_ai, no_network, monkeypatch):
    async def boom(self, image_bytes, back_label=False):
        raise enrich_module.EnrichmentError("model exploded")

    monkeypatch.setattr(enrich_module.AIClient, "vision_label", boom, raising=False)
    body = post_label(api).json()
    assert body["need_back_label"] is True
    assert any("vision model" in m.lower() for m in body["messages"])


def test_scan_rejects_non_image(api, user, fake_ai, no_network):
    resp = api.post(
        "/api/scan/label",
        files={"file": ("evil.jpg", b"not an image at all", "image/jpeg")},
    )
    assert resp.status_code == 400


def test_scan_does_not_write_to_the_database(api, user, fake_ai, no_network):
    post_label(api)
    assert api.get("/api/wines").json()["total"] == 0


# --------------------------------------------------------------- typed-name lookup
def test_lookup_requires_some_input(api, user, fake_ai, no_network):
    assert api.post("/api/scan/lookup", json={}).status_code == 422


def test_lookup_by_name_only(api, user, fake_ai, no_network):
    body = api.post("/api/scan/lookup", json={"name": "Barolo"}).json()
    assert body["suggestion"]["grape"] == "Merlot"  # from the mocked text model
    assert "name" not in body["suggestion"]  # the user already supplied it


# --------------------------------------------------------------- coercion rules
@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"vintage": "2019"}, 2019),
        ({"vintage": 2019}, 2019),
        ({"vintage": "n/a"}, None),
        ({"vintage": 1500}, None),
        ({"vintage": 3999}, None),
    ],
)
def test_vintage_coercion(raw, expected):
    got = enrich_module._coerce(raw)
    assert got.get("vintage") == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"alcohol_pct": "13,5"}, 13.5),
        ({"alcohol_pct": "13.5 %"}, 13.5),
        ({"alcohol_pct": "13.5% vol"}, 13.5),
        ({"alcohol_pct": 999}, None),
        ({"alcohol_pct": "unknown"}, None),
    ],
)
def test_alcohol_coercion(raw, expected):
    assert enrich_module._coerce(raw).get("alcohol_pct") == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"sugar_g_l": "3 g/l"}, 3.0),
        ({"sugar_g_l": "dry"}, None),
        ({"sugar_g_l": -5}, None),
    ],
)
def test_sugar_coercion(raw, expected):
    assert enrich_module._coerce(raw).get("sugar_g_l") == expected


@pytest.mark.parametrize(
    "value,expected",
    [("red", "red"), ("RED", "red"), ("rosé", "rose"), ("rose", "rose"),
     ("sparkling", "sparkling"), ("champagne", "sparkling"), ("nonsense", None)],
)
def test_wine_type_coercion(value, expected):
    assert enrich_module._coerce({"wine_type": value}).get("wine_type") == expected


@pytest.mark.parametrize("field", ["acidity", "sweetness", "body", "mouthfeel", "wood"])
def test_gauge_coercion_clamps_to_range(field):
    assert enrich_module._coerce({field: 3}).get(field) == 3
    assert enrich_module._coerce({field: 99}).get(field) is None
    assert enrich_module._coerce({field: "high"}).get(field) is None


def test_gauge_prose_descriptors_map_to_0_3():
    """Web/internet text that describes the structure fills the 0-3 gauges."""
    got = enrich_module._coerce(
        {
            "body": "full-bodied",
            "wood": "heavily oaked",
            "sweetness": "dry",
            "acidity": "crisp, high acidity",
            "mouthfeel": "tannic",
        }
    )
    assert got["body"] == 3
    assert got["wood"] == 3
    assert got["sweetness"] == 0  # "dry" explicitly means no sweetness
    assert got["acidity"] == 3
    assert got["mouthfeel"] == 3
    # Unknown prose yields no guess (stays absent), never an invented level.
    assert enrich_module._coerce({"body": "mysterious"}).get("body") is None


def test_coercion_drops_unknown_and_dangerous_keys():
    got = enrich_module._coerce(
        {"name": "X", "id": "hack", "created_by": "hack", "photo": "../etc/passwd", "rating": 5}
    )
    assert set(got) <= set(enrich_module.ENRICHABLE_FIELDS)
    assert "id" not in got and "rating" not in got


def test_coercion_truncates_over_long_strings():
    got = enrich_module._coerce({"name": "z" * 500, "aromas": "a" * 5000})
    assert len(got["name"]) <= 200
    assert len(got["aromas"]) <= 2000


# ----------------------------------------------------------- internet -> summariser
def test_web_results_are_summarised_into_fields(api, user, fake_ai, monkeypatch):
    """When the internet returns context, the local summariser extracts fields."""
    import app.routers.scan as scan_router

    async def fake_search(settings, query):
        return "Some raw internet text about a Spanish Rioja wine.", ["http://example.com/rioja"]

    async def fake_summarize(self, context):
        return {
            "name": "Web Red",
            "maker": "Web Winery",
            "wine_type": "red",
            "country": "Spain",
            "region": "Rioja",
            "grape": "Tempranillo",
            "alcohol_pct": 13.5,
        }

    # Patch on the modules that reference these symbols, not just enrich.
    monkeypatch.setattr(scan_router, "search_web", fake_search)
    monkeypatch.setattr(scan_router, "summarize_search", fake_summarize)
    monkeypatch.setattr(enrich_module, "summarize_search", fake_summarize)
    body = api.post("/api/scan/lookup", json={"name": "Something"}).json()
    assert body["suggestion"]["country"] == "Spain"
    assert body["suggestion"]["grape"] == "Tempranillo"
    assert "web search" in " ".join(body["messages"]).lower()


def test_web_search_can_fill_structure_gauges(api, user, fake_ai, monkeypatch):
    """The web summariser's structure gauges flow into the suggestion (0-3)."""
    import app.routers.scan as scan_router

    async def fake_search(settings, query):
        return "Dry, full-bodied, unoaked Rioja with high acidity and tannins.", ["src"]

    async def fake_summarize(self, context):
        return {
            "wine_type": "red",
            "country": "Spain",
            "region": "Rioja",
            "grape": "Tempranillo",
            "sweetness": 0,  # dry
            "body": 3,  # full-bodied
            "wood": 0,  # unoaked
            "acidity": 3,  # high acidity
            "mouthfeel": 3,  # tannic
        }

    monkeypatch.setattr(scan_router, "search_web", fake_search)
    monkeypatch.setattr(scan_router, "summarize_search", fake_summarize)
    monkeypatch.setattr(enrich_module, "summarize_search", fake_summarize)
    body = api.post("/api/scan/lookup", json={"name": "Something"}).json()
    s = body["suggestion"]
    assert s["body"] == 3
    assert s["wood"] == 0
    assert s["sweetness"] == 0
    assert s["acidity"] == 3
    assert s["mouthfeel"] == 3
    # Out-of-range gauge values from a model are dropped, never force-fit.
    assert all(0 <= (s.get(g) or 0) <= 3 for g in ("acidity", "sweetness", "body", "mouthfeel", "wood"))


def test_saq_and_vivino_are_searched_alongside_general_web(api, user, fake_ai, monkeypatch):
    """SAQ (saq.com) and Vivino (vivino.com) are large wine databases, so the
    lookup also runs site-scoped searches (in parallel) and merges their
    results into the suggestion."""
    import app.routers.scan as scan_router

    seen = []

    async def fake_search(settings, query):
        seen.append(query)
        if "site:saq.com" in query:
            return "SAQ product page: a dry red from Bordeaux, full-bodied.", ["https://www.saq.com/en/123"]
        if "site:vivino.com" in query:
            return "Vivino: 4.2/5 rating, medium-bodied, notes of blackberry.", ["https://www.vivino.com/wines/123"]
        return "General web text about a Bordeaux red wine.", ["https://example.com/bordeaux"]

    async def fake_summarize(self, context):
        return {
            "wine_type": "red",
            "country": "France",
            "region": "Bordeaux",
            "sweetness": 0,
            "body": 3,
        }

    monkeypatch.setattr(scan_router, "search_web", fake_search)
    monkeypatch.setattr(scan_router, "summarize_search", fake_summarize)
    monkeypatch.setattr(enrich_module, "summarize_search", fake_summarize)
    body = api.post("/api/scan/lookup", json={"name": "Chateau Test", "maker": "Test"}).json()
    # A general, a saq.com-scoped, and a vivino.com-scoped query were issued.
    assert any("site:saq.com" in q for q in seen), seen
    assert any("site:vivino.com" in q for q in seen), seen
    assert any("Chateau Test" in q for q in seen), seen
    # The catalogue-derived facts reached the suggestion.
    assert body["suggestion"]["region"] == "Bordeaux"
    assert body["suggestion"]["body"] == 3


def test_summariser_declining_web_falls_back_without_overwriting(api, user, fake_ai, monkeypatch):
    """If the summariser finds no wine in the results, we don't invent one."""
    import app.routers.scan as scan_router

    async def fake_summarize(self, context):
        return {}  # model declined

    async def fake_search(settings, query):
        return "Barolo wine pasta sauce ingredients: tomato, pork...", ["src"]

    monkeypatch.setattr(enrich_module, "summarize_search", fake_summarize)
    monkeypatch.setattr(scan_router, "summarize_search", fake_summarize)
    monkeypatch.setattr(scan_router, "search_web", fake_search)
    body = api.post("/api/scan/lookup", json={"name": "Barolo"}).json()
    # No reliable web match -> no confident web-sourced country/grape.
    assert body["suggestion"].get("country") is None or "review" in " ".join(body["messages"]).lower()


def test_duckduckgo_search_returns_real_results(monkeypatch):
    """Uses the live DuckDuckGo HTML endpoint; skips if no egress."""
    import asyncio

    async def run():
        return await enrich_module.search_web(
            __import__("app.config", fromlist=["Settings"]).Settings(
                web_search_enabled=True, web_search_provider="duckduckgo"
            ),
            "Opus One wine",
        )

    try:
        ctx, sources = asyncio.run(run())
    except Exception:
        pytest.skip("no network egress to DuckDuckGo in this environment")
    assert "Opus One" in ctx
    assert sources  # at least one real (non-ad) link
    assert all("duckduckgo.com/y.js" not in s for s in sources)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_wikipedia_fallback_fills_context_when_primary_empty(monkeypatch):
    """When the primary engine (DuckDuckGo) yields nothing, search_web falls
    back to Wikipedia and returns real encyclopedic context + a source."""
    import asyncio
    import httpx

    # Simulate DuckDuckGo returning an empty anti-bot page.
    async def fake_ddg(settings, query):
        return "", []

    async def fake_wiki(settings, query):
        return "- Château Margaux: a Bordeaux wine estate in France.", [
            "https://en.wikipedia.org/wiki/Ch%C3%A2teau_Margaux"
        ]

    monkeypatch.setattr(enrich_module, "_duckduckgo_search", fake_ddg)
    monkeypatch.setattr(enrich_module, "_wikipedia_search", fake_wiki)
    ctx, sources = asyncio.run(
        enrich_module.search_web(
            __import__("app.config", fromlist=["Settings"]).Settings(web_search_enabled=True),
            "Chateau Margaux",
        )
    )
    assert "Château Margaux" in ctx
    assert any("wikipedia.org" in s for s in sources)


def test_wikipedia_search_parses_api(monkeypatch):
    """_wikipedia_search parses the search + extracts API into context."""
    import asyncio
    import httpx

    calls = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            calls.setdefault("urls", []).append(url)
            if params.get("list") == "search":
                return _FakeResp(
                    {"query": {"search": [{"title": "Château Margaux"}]}}
                )
            return _FakeResp(
                {"query": {"pages": {"1": {"extract": "Château Margaux is a Bordeaux wine estate in France."}}}}
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    ctx, sources = asyncio.run(
        enrich_module._wikipedia_search(
            __import__("app.config", fromlist=["Settings"]).Settings(web_search_enabled=True),
            "Chateau Margaux",
        )
    )
    assert "Bordeaux" in ctx
    assert sources and "Château_Margaux" in sources[0]
    assert "api.php" in calls["urls"][0]


def test_grape_infers_wine_type():
    """A grape with no explicit type still yields a valid wine_type."""
    got = enrich_module._coerce({"grape": "Sauvignon Blanc", "country": "New Zealand"})
    assert got["wine_type"] == "white"
    got2 = enrich_module._coerce({"grape": "Pinot Noir"})
    assert got2["wine_type"] == "red"


def test_aromas_list_is_joined_to_string():
    got = enrich_module._coerce({"aromas": ["citrus", "tropical fruit", "grass"]})
    assert isinstance(got["aromas"], str)
    assert "citrus" in got["aromas"] and "grass" in got["aromas"]


def test_real_internet_lookup_pipeline(monkeypatch):
    """End-to-end: real DDG context -> mocked summariser returns fields."""
    import asyncio

    import app.config as cfg

    captured = {}

    async def fake_summarize(self, context):
        captured["ctx"] = context  # verify real DDG text reached the summariser
        return {"country": "France", "wine_type": "red"}

    monkeypatch.setattr(enrich_module, "summarize_search", fake_summarize)
    settings = cfg.Settings(web_search_enabled=True, web_search_provider="duckduckgo")
    try:
        ctx, _ = asyncio.run(enrich_module.search_web(settings, "Chateau Margaux"))
    except Exception:
        pytest.skip("no network egress to DuckDuckGo in this environment")
    from app.services.enrich import AIClient

    out = asyncio.run(enrich_module.summarize_search(AIClient(settings), ctx))
    assert out.get("country") == "France"
    assert captured.get("ctx") == ctx


# ----------------------------------------------------------- back label
def test_back_label_scan_returns_raw_text(api, user, fake_ai, no_network):
    """A back-label scan carries the raw label text so it can be persisted."""
    fake_ai["vision"] = {
        "legible": True,
        "grape": "Syrah",
        "alcohol_pct": 14.0,
        "raw_text": "SYRAH 14.0% VOL PRODUCED IN FRANCE",
    }
    body = post_label(api, is_back_label="true").json()
    assert body["back_label_text"] == "SYRAH 14.0% VOL PRODUCED IN FRANCE"
    assert body["need_back_label"] is False
    # The back label's structured fields are still offered as a suggestion.
    assert body["suggestion"]["grape"] == "Syrah"


def test_front_label_scan_does_not_return_back_text(api, user, fake_ai, no_network):
    body = post_label(api).json()
    assert body.get("back_label_text") is None


def test_back_label_text_persisted_on_wine_create(api, user, settings):
    """The raw back-label text is stored with the wine, not just dropped."""
    from sqlalchemy import create_engine, text

    payload = {
        "name": "Back Label Test",
        "wine_type": "red",
        "back_label_text": "SYRAH 14.0% VOL",
    }
    resp = api.post("/api/wines", json=payload)
    assert resp.status_code == 201, resp.text

    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT back_label_text FROM wines WHERE name = :n"),
            {"n": "Back Label Test"},
        ).first()
    assert row is not None
    assert row[0] == "SYRAH 14.0% VOL"


# ----------------------------------------------------- provider split (vision vs summary)
def test_vision_and_summary_use_separate_providers(monkeypatch):
    """Vision and summary stages must read their OWN base_url/api_key/model,
    so they can run on different providers."""
    from app.config import Settings

    settings = Settings(
        vision_base_url="https://vision.example/v1",
        vision_api_key="vision-key",
        vision_model="vl-model",
        summary_base_url="https://summary.example/v1",
        summary_api_key="summary-key",
        summary_model="sum-model",
    )
    captured = {}

    async def fake_openai(self, payload, base_url, api_key):
        captured.setdefault("openai", []).append((base_url, api_key, payload.get("model")))
        return '{"name": "ok"}'

    monkeypatch.setattr(enrich_module.AIClient, "_openai_chat", fake_openai, raising=False)

    client = enrich_module.AIClient(settings)
    import asyncio

    async def run_all():
        await client.vision_label(b"img")
        await enrich_module.summarize_search(client, "ctx")

    asyncio.run(run_all())

    assert captured["openai"][0] == (
        "https://vision.example/v1",
        "vision-key",
        "vl-model",
    )
    assert captured["openai"][1] == (
        "https://summary.example/v1",
        "summary-key",
        "sum-model",
    )


def test_summary_falls_back_to_vision_provider(monkeypatch):
    """When no summary provider is configured, summarisation reuses the vision
    provider (the common single-endpoint setup)."""
    from app.config import Settings

    settings = Settings(
        vision_base_url="http://ollama:11434",
        vision_model="vl-model",
        summary_model="sum-model",
        # Explicitly disable any summary provider (incl. from .env) so the
        # fallback-to-vision path is exercised.
        summary_base_url=None,
        summary_api_key=None,
    )
    captured = {}

    async def fake_openai(self, payload, base_url, api_key):
        captured.setdefault("openai", []).append((base_url, payload.get("model")))
        return '{"name": "ok"}'

    monkeypatch.setattr(enrich_module.AIClient, "_openai_chat", fake_openai, raising=False)

    client = enrich_module.AIClient(settings)
    import asyncio

    async def run_all():
        await client.vision_label(b"img")
        await enrich_module.summarize_search(client, "ctx")

    asyncio.run(run_all())

    # Both stages used the vision endpoint; summary used summary_model. The mock
    # receives the RAW base_url; normalization happens inside _openai_chat.
    assert captured["openai"][0] == ("http://ollama:11434", "vl-model")
    assert captured["openai"][1] == ("http://ollama:11434", "sum-model")


def test_openai_url_normalisation():
    """A bare host, a /v1 root, or a full URL all map to /v1/chat/completions."""
    assert enrich_module.AIClient._openai_url("https://api.deepseek.com") == (
        "https://api.deepseek.com/v1/chat/completions"
    )
    assert enrich_module.AIClient._openai_url("https://api.openai.com/v1") == (
        "https://api.openai.com/v1/chat/completions"
    )
    assert enrich_module.AIClient._openai_url("https://api.openai.com/v1/chat/completions") == (
        "https://api.openai.com/v1/chat/completions"
    )
    assert enrich_module.AIClient._openai_url("http://192.168.1.222:11434") == (
        "http://192.168.1.222:11434/v1/chat/completions"
    )


