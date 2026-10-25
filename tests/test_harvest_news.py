"""Live agri news feed: feed parsing, dedupe, tagging, the /harvest/api/news filters and the poller switches. Offline."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.harvest import market, news
from agent.routes import public_mode
from agent.server import app

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>"wheat price" - Google News</title>
<item><title>Wheat futures jump as Russia weighs grain export ban - Reuters</title>
  <link>https://news.google.com/rss/articles/AAA?oc=5</link><pubDate>Sat, 25 Oct 2026 08:00:00 GMT</pubDate>
  <source url="https://www.reuters.com">Reuters</source></item>
<item><title>Spain olive oil harvest seen lower after drought in Jaen - Olive Oil Times</title>
  <link>https://news.google.com/rss/articles/BBB?oc=5</link><pubDate>Fri, 24 Oct 2026 10:30:00 +0200</pubDate>
  <source url="https://www.oliveoiltimes.com">Olive Oil Times</source></item>
<item><title>Cocoa prices slide as Ivory Coast crop improves</title>
  <link>https://example.com/cocoa</link><pubDate>Thu, 23 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>No link here</title></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Grain wire</title>
<entry><title>Italy declares drought emergency as durum fields wither</title>
  <link rel="alternate" href="https://example.com/italy-drought"/><updated>2026-10-22T12:00:00Z</updated></entry>
</feed>"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    news._status["llm_calls"] = 0
    market.reset_live_book()
    yield
    market.reset_live_book()


def _seed() -> int:
    return news.store(news.parse_feed(RSS, "Google News: wheat price") + news.parse_feed(ATOM, "Grain wire"))


def test_parse_rss_and_atom():
    items = news.parse_feed(RSS, "Google News: wheat price")
    assert [i["title"] for i in items] == ["Wheat futures jump as Russia weighs grain export ban",
                                           "Spain olive oil harvest seen lower after drought in Jaen",
                                           "Cocoa prices slide as Ivory Coast crop improves"]
    assert [i["source"] for i in items] == ["Reuters", "Olive Oil Times", "Google News"]
    assert items[0]["published_at"] == "2026-10-25T08:00:00Z" and items[1]["published_at"] == "2026-10-24T08:30:00Z"
    atom = news.parse_feed(ATOM, "Grain wire")
    assert atom == [{"title": "Italy declares drought emergency as durum fields wither", "url": "https://example.com/italy-drought",
                     "published_at": "2026-10-22T12:00:00Z", "source": "Grain wire"}]
    with pytest.raises(ValueError):
        news.parse_feed(b"<html>not a feed", "x")
    with pytest.raises(ValueError):
        news.parse_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss/>', "x")


def test_dedupe_by_url_and_title_and_trim():
    assert _seed() == 4
    assert _seed() == 0
    again = [{"title": "Wheat futures JUMP as Russia weighs grain export-ban", "url": "https://other.example/x", "source": "Other", "published_at": None},
             {"title": "Brand new headline", "url": "https://news.google.com/rss/articles/AAA?oc=5", "source": "x", "published_at": None}]
    assert news.store(again) == 0
    assert news.store([{"title": "Tractor of the Year shortlist", "url": "https://example.com/t", "source": "x", "published_at": None}]) == 0
    many = [{"title": f"Rice note {i}", "url": f"https://example.com/r{i}", "source": "x", "published_at": None} for i in range(10)]
    assert news.store(many, keep=6) == 10
    rows, latest = news.query(limit=100)
    assert len(rows) == 6 and latest == max(r["id"] for r in rows)


def test_tagging_matches_bank_commodities_regions_and_markets():
    c, r, s = news.tag("Wheat futures jump as Russia weighs grain export ban")
    assert "wheat" in c and "Russia" in r
    assert {"wheat-up10-6m", "wheat-down10-3m", "wheat-above300-6m", "ru-grain-ban-2027"} <= set(s)
    c, r, s = news.tag("Spain olive oil harvest seen lower after drought in Jaen")
    assert {"olive oil", "water"} <= set(c) and r == ["Spain"]
    assert "es-olive-output-2027" in s and "olive-up15-6m" in s and not any(x.startswith("wheat") for x in s)
    assert news.tag("Saudi fund buys 12,000 ha of Romanian farmland")[2] == ["gulf-eu-farmland-2027"]
    assert news.tag("EU CAP reform to fund farm heat adaptation")[2] == ["eu-cap-heat-2028"]
    assert news.tag("Cap and gown season")[0] == []  # CAP only in capitals
    assert news.tag("Athens imposes water rationing")[2] == ["gr-water-rationing-2027"]
    assert news.tag("Tech stocks rally") == ([], [], [])


@pytest.fixture()
def client():
    return TestClient(app)


def test_api_filters_by_commodity_qid_and_since(client):
    _seed()
    body = client.get("/harvest/api/news").json()
    assert len(body["items"]) == 4 and body["latest_id"] == 4
    first = body["items"][0]
    assert set(first) == {"id", "title", "source", "url", "published_at", "commodities", "regions", "qids", "summary", "direction"}
    assert first["title"].startswith("Wheat futures")  # newest first
    assert "ru-grain-ban-2027" in first["qids"] and any(q.startswith("wheat-up10-6m@") for q in first["qids"])
    olive = client.get("/harvest/api/news", params={"commodity": "olive oil"}).json()["items"]
    assert [i["title"] for i in olive] == ["Spain olive oil harvest seen lower after drought in Jaen"]
    hard_qid = next(q for q in first["qids"] if q.startswith("wheat-up10-6m@"))
    by_qid = client.get("/harvest/api/news", params={"qid": hard_qid}).json()["items"]
    assert {i["title"] for i in by_qid} == {"Wheat futures jump as Russia weighs grain export ban",
                                            "Italy declares drought emergency as durum fields wither"}
    soft = client.get("/harvest/api/news", params={"qid": "it-drought-2027"}).json()["items"]
    assert [i["title"] for i in soft] == ["Italy declares drought emergency as durum fields wither"]
    assert client.get("/harvest/api/news", params={"since": 4}).json()["items"] == []
    assert len(client.get("/harvest/api/news", params={"limit": 1}).json()["items"]) == 1
    doc = client.get("/harvest/api/agents").json()
    assert any(e["path"].startswith("/harvest/api/news") for e in doc["endpoints"])
    assert public_mode.is_public("GET", "/harvest/api/news") and public_mode.is_public("GET", "/harvest/news")
    page = client.get("/harvest/news")
    assert page.status_code == 200 and "/harvest/api/news" in page.text


def test_poll_once_stores_and_survives_a_failing_source(monkeypatch):
    def fake(name, url, client):
        if name == "bad":
            raise httpx.ConnectTimeout("down")
        return news.parse_feed(RSS if name == "rss" else ATOM, name)

    monkeypatch.setattr(news, "fetch_source", fake)
    seen = news.poll_once([("rss", "https://a"), ("bad", "https://b"), ("atom", "https://c")])
    assert seen == {"rss": 3, "bad": -1, "atom": 1}
    rows, latest = news.query()
    assert latest == 4 and news.status()["last_added"] == 4
    assert rows[-1]["title"].startswith("Italy declares")  # oldest gets the lowest id


def test_poller_disabled_offline_or_zero_interval(monkeypatch):
    assert news.ensure_poller() is False
    monkeypatch.setenv("HARVEST_OFFLINE", "0")
    monkeypatch.setenv("NEWS_POLL_S", "0")
    assert news.ensure_poller() is False


def test_enrich_once_adds_summary_and_direction_and_caps(monkeypatch):
    _seed()
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return json.dumps({"items": [{"id": 1, "why": "Export curbs tighten EU wheat supply", "direction": "up"},
                                     {"id": 2, "why": "bad", "direction": "sideways"}]})

    assert news.enrich_once(ask) == 1
    assert "Wheat futures jump" in prompts[0]
    rows = {r["id"]: r for r in news.query()[0]}
    assert rows[1]["summary"] == "Export curbs tighten EU wheat supply" and rows[1]["direction"] == "up"
    assert rows[2]["summary"] is None
    assert news.enrich_once(ask) == 0 and len(prompts) == 1  # everything already looked at
    monkeypatch.setenv("NEWS_LLM_MAX_CALLS", "1")
    news.store([{"title": "Rice export ban lifted", "url": "https://example.com/rice", "source": "x", "published_at": None}])
    assert news.enrich_once(ask) == 0 and len(prompts) == 1  # budget spent
    assert "[up: Export curbs tighten EU wheat supply]" in news.headlines_for(["ru-grain-ban-2027"])["ru-grain-ban-2027"][0]
