"""Growth and openness: share cards, the humans-vs-AI season, the bot SDK page and the open-data export. Offline."""

import io
import json
import runpy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.harvest import growth, market
from agent.routes import public_mode
from agent.server import app

EXAMPLE_BOT = Path(__file__).resolve().parents[1] / "agent" / "harvest" / "static" / "sdk" / "example_bot.py"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://crophecy.example")
    market.reset_live_book()
    yield TestClient(app)
    market.reset_live_book()


def first_qid(client) -> str:
    return next(m["qid"] for m in client.get("/harvest/api/markets").json()["markets"] if m["kind"] == "hard")


def register(client, name="Test Bot") -> dict:
    r = client.post("/harvest/api/agents/register", json={"name": name, "world_model": "Prices mean-revert."})
    assert r.status_code == 200, r.text
    return r.json()


def bet(client, token, qid, side="YES", spend=20, **extra) -> dict:
    r = client.post("/harvest/api/bet", json={"token": token, "qid": qid, "side": side, "spend": spend, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_take_phrases():
    assert growth.take("Will wheat (now $247/mt) be above $272/mt by 2027-03?", 0.72) == "72% wheat up"
    assert growth.take("Will olive oil (now $6,205/mt) be below $5,585/mt by 2026-12?", 0.4) == "40% olive oil down"
    assert growth.take("Will Italy declare a national drought emergency in 2027?", 0.52) == "52% YES"


def test_market_card_json_and_png(client):
    qid = first_qid(client)
    card = client.get(f"/harvest/api/card/market/{qid}").json()
    assert card["headline"].startswith("The market says ") and card["tagline"] == "Every harvest has its prophets. We score them."
    assert card["image_url"] == f"https://crophecy.example/harvest/og/market/{qid}.png"
    r = client.get(f"/harvest/og/market/{qid}.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (1200, 630)
    assert client.get("/harvest/og/market/nope@1999-01.png").status_code == 404
    assert client.get("/harvest/api/card/market/nope").status_code == 404


def test_market_page_has_open_graph_tags(client):
    qid = first_qid(client)
    html = client.get(f"/app/market/{qid}").text
    assert f'<meta property="og:image" content="https://crophecy.example/harvest/og/market/{qid}.png">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert f'<link rel="canonical" href="https://crophecy.example/app/market/{qid}">' in html
    assert "/harvest/static/app/share.js" in html and html.count("<title>") == 1
    assert "og:image" not in client.get("/app/market/nope").text  # unknown market: plain page, no tags


def test_agent_card_png_and_page(client):
    accounts = client.get("/harvest/api/leaderboard").json()["accounts"]
    house = next(a for a in accounts if a["category"] in ("verified", "house"))
    card = client.get(f"/harvest/api/card/agent/{house['username']}").json()
    assert house["name"].split(" · ")[0] in card["headline"] and card["url"] == f"https://crophecy.example/app/agent/{house['username']}"
    r = client.get(f"/harvest/og/agent/{house['id']}.png")
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (1200, 630)
    page = client.get(f"/app/agent/{house['username']}").text
    assert 'property="og:title"' in page and "/harvest/static/app/agent.js" in page
    assert client.get("/harvest/og/agent/nobody.png").status_code == 404


def test_season_ranks_humans_against_agents(client):
    qid = first_qid(client)
    human = client.post("/harvest/api/join", json={"nickname": "Ann", "username": "ann-h"}).json()
    agent = register(client)
    bet(client, human["token"], qid, "YES", 50)
    bet(client, agent["token"], qid, "NO", 30)
    s = client.get("/harvest/api/season").json()
    assert s["headline"] == "Can you beat the FAO bot?" and s["totals"]["humans"] >= 1
    by_id = {r["id"]: r for r in s["rows"]}
    assert by_id[human["user_id"]]["type"] == "human" and by_id[human["user_id"]]["icon"] == "👤"
    assert by_id[agent["agent_id"]]["type"] == "agent" and by_id[agent["agent_id"]]["metric"] == "pnl"
    assert [r["rank"] for r in s["rows"]] == list(range(1, len(s["rows"]) + 1))
    pnls = [r["pnl"] for r in s["rows"]]
    assert pnls == sorted(pnls, reverse=True)
    assert {r["type"] for r in client.get("/harvest/api/season?type=human").json()["rows"]} == {"human"}
    assert client.get("/harvest/api/season?type=cat").status_code == 400


def test_season_uses_brier_once_a_market_resolves(client):
    qid = first_qid(client)
    sure = register(client, "Sure")
    unsure = register(client, "Unsure")
    bet(client, sure["token"], qid, "YES", 10, p_yes=0.9)
    bet(client, unsure["token"], qid, "NO", 10, p_yes=0.2)
    book = market.live_book()
    with book.lock:
        book.resolve(qid, 1)
    rows = {r["id"]: r for r in client.get("/harvest/api/season").json()["rows"]}
    assert rows[sure["agent_id"]]["metric"] == "brier" and rows[sure["agent_id"]]["brier"] == pytest.approx(0.01)
    assert rows[unsure["agent_id"]]["brier"] == pytest.approx(0.64)
    assert rows[sure["agent_id"]]["rank"] < rows[unsure["agent_id"]]["rank"]
    metrics = [r["metric"] for r in sorted(rows.values(), key=lambda r: r["rank"])]
    assert metrics == sorted(metrics, key=lambda m: m != "brier")  # every Brier-scored row ranks above every P&L-only row


def test_developers_reference_is_generated_from_routes(client):
    d = client.get("/harvest/api/developers").json()
    paths = {(e["method"], e["path"]): e for e in d["endpoints"]}
    for key in (("POST", "/harvest/api/agents/bet"), ("GET", "/harvest/api/agents/markets"), ("GET", "/harvest/api/news"), ("POST", "/harvest/api/byo"),
                ("GET", "/harvest/api/season"), ("GET", "/harvest/api/export/forecasts.csv")):
        assert paths[key]["available"], key
        assert paths[key]["curl"].startswith("curl")
    assert d["base_url"] == "https://crophecy.example" and d["open_data"]["name"] == "CC-BY-4.0"
    assert d["rate_limits"]["bet"] and "token" in d["auth"]
    assert client.get("/harvest/static/sdk/example_bot.py").status_code == 200
    assert 'data-page="developers"' in client.get("/app/developers").text


def test_example_bot_places_a_bet(client, monkeypatch):
    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(req, timeout=None):
        path = req.full_url.removeprefix("http://127.0.0.1:3001")
        r = client.post(path, content=req.data, headers={"content-type": "application/json"}) if req.data else client.get(path)
        assert r.status_code == 200, r.text
        return Reply(r.content)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.delenv("CROPHECY_URL", raising=False)
    before = len(client.get("/harvest/api/state").json()["recent_trades"])
    runpy.run_path(str(EXAMPLE_BOT))
    trades = client.get("/harvest/api/state").json()["recent_trades"]
    assert len(trades) == before + 1 and trades[-1]["trader"].startswith("ext:fade-bot-")


def test_export_forecasts_and_resolutions(client):
    qid = first_qid(client)
    human = client.post("/harvest/api/join", json={"nickname": "Bo", "username": "bo-h"}).json()
    bet(client, human["token"], qid, "YES", 15, p_yes=0.7, rationale="dry spring")
    csv_text = client.get("/harvest/api/export/forecasts.csv")
    assert csv_text.status_code == 200 and csv_text.headers["x-license"] == "CC-BY-4.0"
    header, *lines = csv_text.text.splitlines()
    assert header == ",".join(growth.FORECAST_COLUMNS)
    assert any(",bet," in "," + ln or ln.startswith("bet,") for ln in lines) and "dry spring" in csv_text.text
    assert human["token"] not in csv_text.text and "token" not in header
    js = client.get("/harvest/api/export/forecasts.json").json()
    assert js["license"]["name"] == "CC-BY-4.0" and js["count"] == len(js["forecasts"]) > 0
    kinds = {r["record"] for r in js["forecasts"]}
    assert {"bet", "backtest_forecast"} <= kinds
    res = client.get("/harvest/api/export/resolutions.csv").text.splitlines()
    assert res[0] == ",".join(growth.RESOLUTION_COLUMNS) and len(res) > 1
    assert client.get("/harvest/api/export").json()["name"] == "CC-BY-4.0"


@pytest.mark.parametrize("path", ["/harvest/og/market/x.png", "/harvest/og/agent/x.png", "/harvest/api/season", "/harvest/api/developers",
                                   "/harvest/api/export/forecasts.csv", "/app/season", "/app/developers", "/app/agent/x"])
def test_public_in_public_mode(path):
    assert public_mode.is_public("GET", path)
