"""Build a static snapshot of The Crophecy for GitHub Pages (or any static host) from a running server.

    python scripts/build_static.py --base-url http://127.0.0.1:3001 --out site

It saves every GET response the site pages read (frontend/index.html, frontend/js/api.js, croppy.js) under <out>/api/,
the most recent agent bets for a looped replay, Croppy's and the farm chat's answers to their suggested questions and to
about twenty more likely ones each, the sealed-holdout backtest of each preset theory, and the page itself with every
absolute URL made relative, so the copy works under a sub-path such as /crophecy-site/.
The page sets window.CROPHECY_STATIC, which switches api.js into static mode: reads come from api/*.json, the
recorded bets replay at their real pace instead of polling, and trading (bet, sign-up, follow and copy) is a demo held in
the page's memory on a port of the LMSR book, which resets on reload. A chat question gets the recorded answer to the
closest recorded question; the theory page backtests the preset theories from their recorded runs. WhatsApp shows as
off (no conversation is recorded to show). The waitlist keeps working (it posts straight to Supabase).
Routes are hash routes (#live, #markets, #m-<qid>); /live etc. redirect to them through small stub pages and 404.html.
Nothing private is copied: test accounts are already left out of the feeds, and keys that look like credentials or
contact details are dropped from every saved response.
"""

import argparse
import json
import logging
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
STATIC = ROOT / "agent" / "harvest" / "static"
WAITLIST = ROOT / "waitlist"
CROPPY_SUGGEST = ("What is this site?", "Which AI is winning right now?", "How do I play?")  # croppy.js SUGGEST
CROPPY_ASKS = (*CROPPY_SUGGEST, "What is a world model?", "What is a Brier score?", "How does the leaderboard work?",
               "Is this real money?", "How do prices move when I bet?", "How do you stop the AI from knowing the answers?", "How are markets resolved?",
               "What is the sealed holdout?", "Can I write my own theory?", "How do I follow or copy an agent?",
               "What does Solana have to do with this?", "Which market is traded the most?", "Do the AI agents beat the market?",
               "How can my own AI agent trade here?", "Which agent should I follow?", "Which markets are about olive oil?",
               "Can I sell my position?", "Is this financial advice?", "Who built The Crophecy?", "How do I get more credits?")
FARM_SUGGEST = ("I grow olives near Jaén. Will prices fall?", "Should I sell my wheat now or store it?",
                "Will drought hit maize in Hungary?", "Is it a bad year for rice in Spain?")  # api.js FARM_ASKS
FARM_ASKS = (*FARM_SUGGEST, "Will olive oil prices rise next year?", "Will wheat prices go up?", "Is a drought coming in Italy?",
             "Should I hedge my sunflower crop?", "Will Spain have a good olive harvest?", "Will maize prices fall?",
             "Will rice prices rise?", "Is there a risk of water rationing in Greece?", "I grow wheat in France. What should I watch?",
             "Will Russia ban grain exports?", "How sure are the agents about wheat?", "Will cocoa prices keep rising?",
             "I grow sunflowers in Romania. What is the outlook?", "What does the market say about sugar beet?",
             "I farm olives in Greece. Should I worry about the heat?", "When should I sell my olive oil?",
             "Will coffee prices go up?", "Is farmland a good buy right now?")
THEORIES = (  # the theory page's presets in static mode; the first is the form's default text in api.js renderTheory
    {"name": "Heat bites first", "world_model": "Heat waves and droughts hit southern Europe harder every year, and markets are slow to price it in. "
     "When stocks are thin, a single bad harvest pushes prices up fast, and prices that start rising keep rising for a while."},
    {"name": "The dollar decides", "world_model": "Food commodities are priced in dollars, so a strong dollar and high interest rates pull prices down "
     "and a weak dollar lifts them. Weather matters less than people think: global supply adjusts within a season, and prices drift back to their long-run average."},
    {"name": "Politics moves first", "world_model": "Export bans, wars and subsidies move food prices more than the weather. When a big exporter such as "
     "Russia, Ukraine or India restricts sales, prices jump within weeks; when governments are calm, prices follow last year's trend and rarely break out."},
)
ROUTES = ("home", "live", "markets", "leaderboard", "theory", "farmer")
PRIVATE_KEY = re.compile(r"token|email|e_mail|password|secret|phone|whatsapp_from|contact|admin|api_key|apikey", re.I)
ENGLISH = {"the", "and", "on", "of", "to", "is", "in", "for", "by", "your", "a", "be", "will", "our", "are", "with", "that"}
JUNK_RATIONALE = re.compile(r"<=\s*\d+\s*chars|^\s*(\.\.\.|…)?\s*$")
LEDGER_LIMIT = 5000  # the most the ledger endpoint returns
logger = logging.getLogger("build_static")


def safe(name: str) -> str:
    """A file-name-safe form of a market id; api.js maps ids with the same rule."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


class Client:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def raw(self, path: str, *, body: dict[str, Any] | None = None, accept: str = "application/json", timeout: float = 120) -> bytes:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Accept": accept, **({"Content-Type": "application/json"} if data is not None else {})}
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the operator's own server
            return r.read()

    def json(self, path: str, *, body: dict[str, Any] | None = None) -> Any:
        return json.loads(self.raw(path, body=body))


def scrub(value: Any) -> Any:
    """Drop keys that look like credentials or contact details, at any depth."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if not PRIVATE_KEY.search(str(k))}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def clean_rationale(text: Any) -> str:
    """Outside agents sometimes echo the prompt's length hint instead of a reason; show no reason rather than that."""
    text = str(text or "").strip()
    body = re.sub(r"^[\w.-]+: ?", "", text)
    return "" if JUNK_RATIONALE.search(body) else text[:280]


def english(text: Any) -> bool:
    """True when common English function words make up a fair share of the text (the farm model sometimes answers in
    the language of the region named in the question, which the snapshot must not keep)."""
    words = re.findall(r"[a-zà-ÿ]+", str(text or "").lower())
    return bool(words) and sum(w in ENGLISH for w in words) / len(words) >= 0.08


def write_json(out: Path, name: str, value: Any) -> None:
    path = out / "api" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scrub(value), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def site_feed(api: Client) -> dict[str, Any]:
    body = api.json("/harvest/api/site")
    for t in body.get("recent_trades", []):
        t["rationale"] = clean_rationale(t.get("rationale"))
    lb = body.get("leaderboard") or {}
    lb["humans"] = [{k: h.get(k) for k in ("user_id", "nickname", "brier", "n", "pnl", "trades")} for h in lb.get("humans", [])]
    return body


def replay_bets(api: Client) -> dict[str, Any]:
    """The latest house and outside agent bets from the ledger, in order, shaped like the feed's recent_trades."""
    entries = api.json(f"/harvest/api/ledger?limit={LEDGER_LIMIT}").get("entries", [])
    last: dict[str, float] = {}
    bets = []
    for e in entries:
        p = e.get("payload") or {}
        if e.get("kind") != "bet" or not str(e.get("trader", "")).startswith(("agent:", "ext:")) or "price_after" not in p:
            continue
        qid, after, spend = p["qid"], float(p["price_after"]), float(p.get("spend") or 0)
        bets.append({"trader": e["trader"], "qid": qid, "side": p.get("side"), "spend": spend, "fee": round(spend * 0.02, 6),
                     "price_before": last.get(qid, after), "price_after": after, "at": e.get("at"), "rationale": clean_rationale(p.get("rationale"))})
        last[qid] = after
    names = {}
    for tid in sorted({b["trader"] for b in bets if b["trader"].startswith("ext:")}):
        try:
            names[tid] = api.json(f"/harvest/api/agents/{urllib.parse.quote(tid, safe='')}").get("name") or tid
        except (urllib.error.URLError, ValueError):
            logger.warning("no name for %s", tid)
    logger.info("replay: %d agent bets from %s to %s", len(bets), bets[0]["at"] if bets else "-", bets[-1]["at"] if bets else "-")
    return {"bets": bets, "names": names}


def canned(api: Client) -> dict[str, Any]:
    """Croppy's and the farm chat's live answers to their suggested and likely questions; links point at the static routes.
    api.js answers any question with the closest of these and offers the suggested ones when none is close."""
    out: dict[str, Any] = {"croppy": {}, "farmer": {}, "suggest": {"croppy": list(CROPPY_SUGGEST), "farmer": list(FARM_SUGGEST)}}
    for q in CROPPY_ASKS:
        for attempt in range(5):  # a reply without "model" is the route's canned fallback (the chat model timed out): ask again
            r = api.json("/harvest/api/croppy", body={"message": q, "history": [], "page": "/"})
            if r.get("model"):
                break
            time.sleep(2 + 2 * attempt)
        else:
            logger.warning("croppy: %s answered only with the canned fallback; not recorded", q)
            continue
        out["croppy"][q] = {"reply": r.get("reply"), "actions": [{"label": a.get("label"), "href": static_href(a.get("href", ""))} for a in r.get("actions") or []]}
        logger.info("croppy: %s (%s)", q, r.get("model") or r.get("source") or "-")
    for q in FARM_ASKS:
        for attempt in range(3):  # ask again, asking for English outright, when the answer drifted into the region's language
            r = api.json("/harvest/api/farmer/chat", body={"message": q + (" Please answer in English." if attempt else "")})
            if english(r.get("reply")):
                break
        else:
            logger.warning("farmer: %s answered only outside English; not recorded", q)
            continue
        out["farmer"][q] = {k: r.get(k) for k in ("reply", "probability", "crop", "region", "confidence")}
        logger.info("farmer: %s (%s)", q, r.get("model") or r.get("source") or "-")
    return out


def theories(api: Client) -> dict[str, Any]:
    """The sealed-holdout backtest of each preset theory, as POST /harvest/api/byo answers it (the server caches a theory by its hash,
    so a rebuild re-reads the recorded run instead of paying for a new one)."""
    keep = ("model_id", "name", "brier_holdout", "n", "rank", "of", "field", "world_model_sha256", "model", "model_label", "source", "rationales")
    presets = []
    for t in THEORIES:
        r = json.loads(api.raw("/harvest/api/byo", body=t, timeout=900))
        if r.get("brier_holdout") is None:
            raise RuntimeError(f"the backtest of {t['name']!r} recorded no Brier score")
        presets.append({**t, "result": {**{k: r.get(k) for k in keep}, "rationales": (r.get("rationales") or [])[:3]}})
        logger.info("theory %s: Brier %.3f over %s questions, rank %s of %s", t["name"], r["brier_holdout"], r.get("n"), r.get("rank"), r.get("of"))
    return {"presets": presets}


def static_href(href: str) -> str:
    """A live-site link (/harvest/board, /market/<qid>, /waitlist/...) as its place in the snapshot, relative to the snapshot's root."""
    fixed = {"/harvest#models": "#leaderboard", "/harvest#markets": "#markets", "/harvest/play": "#markets", "/harvest/play#rank": "#leaderboard",
             "/harvest/board": "#live", "/harvest#holdout": "#leaderboard", "/harvest": "", "/": "", "/home": "", "/app": "#markets"}
    if href in fixed:
        return fixed[href] or "./"
    if href.startswith("/waitlist"):
        return "waitlist/" + href[len("/waitlist"):].lstrip("/")
    if href.startswith("/market/"):
        return "#m-" + urllib.parse.unquote(href[len("/market/"):])
    path = href.lstrip("/").split("#")[0].split("?")[0].removeprefix("app/").split("/")[0]  # /app/live, /app/farmer/chat
    return f"#{path}" if path in ROUTES else "./"


def page(api: Client, snapshot: str) -> str:
    """The live page (index.html with api.js and its hooks, as the server wires it), relative URLs, static flag on."""
    html = api.raw("/", accept="text/html").decode()
    html = re.sub(r'(src|href)="/frontend/assets/', r'\1="assets/', html)
    html = re.sub(r'src="/frontend/js/api\.js', 'src="js/api.js', html)
    html = re.sub(r'(src|href)="/harvest/static/', r'\1="static/', html)
    html = re.sub(r'href="/waitlist/', 'href="waitlist/', html)
    flag = f"<script>window.CROPHECY_STATIC={json.dumps({'root': './', 'snapshot': snapshot})};</script>"
    if "window.CROPHECY_HOOKS=" not in html:
        raise RuntimeError("the served page carries no CROPHECY_HOOKS: is this the Crophecy server?")
    html = html.replace("<script>window.CROPHECY_HOOKS=", flag + "<script>window.CROPHECY_HOOKS=", 1)
    html = html.replace("<title>Crophecy</title>", "<title>Crophecy</title>\n<meta name=\"description\" content=\"The Crophecy: a forecasting market on Europe's harvests where AI world models and people trade. A static snapshot.\">", 1)
    left = sorted(set(re.findall(r'(?:src|href)="(/[^/"][^"]*)"', html)))
    if left:
        raise RuntimeError(f"absolute URLs left in the page: {left[:8]}")
    return html


def waitlist(api: Client) -> str:
    """The live waitlist page (with the Supabase project URL and publishable key filled in), relative URLs, no Croppy."""
    html = api.raw("/waitlist/", accept="text/html").decode()
    html = re.sub(r'(src|href)="/frontend/assets/', r'\1="../assets/', html)
    html = re.sub(r'<script src="/harvest/static/croppy\.js[^"]*"[^>]*></script>\n?', "", html)
    if "charset" not in html[:2000]:  # the server sends the charset as a header; a static host may not
        html = '<meta charset="utf-8">\n<link rel="icon" type="image/svg+xml" href="../assets/crophecy-favicon.svg">\n' + html
    left = sorted(set(re.findall(r'(?:src|href)="(/[^/"][^"]*)"', html)))
    if left:
        raise RuntimeError(f"absolute URLs left in the waitlist page: {left[:8]}")
    return html


def redirect_page() -> str:
    """A stub at /<route>/ (and 404.html for /market/<qid>) that sends the visitor to the hash route on the root page.
    The root is /<repo>/ on a *.github.io project site and / on a custom domain."""
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crophecy</title><meta name="robots" content="noindex"><script>
(function () {
  var p = location.pathname, seg = p.split("/").filter(Boolean);
  var root = /\\.github\\.io$/.test(location.hostname) && seg.length ? "/" + seg.shift() + "/" : "/";
  var rest = seg.join("/").replace(/\\/index\\.html$|^index\\.html$/, "");
  var m = /^market\\/(.+)$/.exec(rest), r = m ? "m-" + decodeURIComponent(m[1]) : rest.split("/")[0];
  location.replace(root + (r ? "#" + r : location.hash));
})();
</script></head><body><p><a href="/">The Crophecy</a></p></body></html>
"""


def copy_assets(out: Path) -> None:
    shutil.copytree(FRONTEND / "assets", out / "assets", dirs_exist_ok=True)
    (out / "js").mkdir(parents=True, exist_ok=True)
    js = (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")
    join = re.search(r'const WA_JOIN = "([^"\n]*)";', js)
    if not join:
        raise RuntimeError("api.js: the WhatsApp join phrase moved; update build_static.py")
    js = js.replace(join.group(0), 'const WA_JOIN = ""; // WhatsApp is paused in the snapshot')
    if join.group(1) in js:
        raise RuntimeError("api.js: the WhatsApp join phrase appears twice; blank it in one place")
    (out / "js" / "api.js").write_text(js, encoding="utf-8")
    (out / "static").mkdir(parents=True, exist_ok=True)
    for name in ("croppy.css", "croppy-face.png", "croppy-256.webp"):
        shutil.copy2(STATIC / name, out / "static" / name)
    croppy = (STATIC / "croppy.js").read_text(encoding="utf-8").replace('"/harvest/static/', '"static/')
    (out / "static" / "croppy.js").write_text(croppy, encoding="utf-8")
    (out / "waitlist").mkdir(parents=True, exist_ok=True)
    for name in ("hero.jpg", "hero-loop.mp4"):
        shutil.copy2(WAITLIST / name, out / "waitlist" / name)


def build(base_url: str, out: Path) -> None:
    api = Client(base_url)
    snapshot = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    if out.exists():
        for child in out.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    body = site_feed(api)
    write_json(out, "site", body)
    write_json(out, "site-anchors", api.json("/harvest/api/site/anchors"))
    write_json(out, "news", api.json("/harvest/api/news?limit=14"))
    write_json(out, "debate", api.json("/harvest/api/debate"))
    write_json(out, "resolutions", api.json("/harvest/api/resolutions"))
    write_json(out, "agents-markets", api.json("/harvest/api/agents/markets"))
    write_json(out, "agents", api.json("/harvest/api/agents"))
    write_json(out, "verify", api.json("/harvest/api/verify"))
    for m in body["markets"]:
        qid = urllib.parse.quote(m["qid"], safe="")
        write_json(out, f"debate/{safe(m['qid'])}", api.json(f"/harvest/api/debate?limit=60&qid={qid}"))
        write_json(out, f"agent-view/{safe(m['qid'])}", api.json(f"/harvest/api/markets/{qid}/agent"))
    write_json(out, "replay-bets", replay_bets(api))
    write_json(out, "canned", canned(api))
    write_json(out, "theories", theories(api))
    (out / "index.html").write_text(page(api, snapshot), encoding="utf-8")
    (out / "waitlist" / "index.html").parent.mkdir(parents=True, exist_ok=True)
    copy_assets(out)
    (out / "waitlist" / "index.html").write_text(waitlist(api), encoding="utf-8")
    (out / "404.html").write_text(redirect_page(), encoding="utf-8")
    for route in (*ROUTES, "app", "harvest", *(f"market/{m['qid']}" for m in body["markets"])):  # deep links answer 200, not 404.html
        (out / route).mkdir(parents=True, exist_ok=True)
        (out / route / "index.html").write_text(redirect_page(), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    (out / "snapshot.json").write_text(json.dumps({"snapshot": snapshot, "markets": len(body["markets"]), "ledger_seq": body.get("seq")}) + "\n", encoding="utf-8")
    logger.info("built %s (snapshot %s, %d markets)", out, snapshot, len(body["markets"]))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base-url", default="http://127.0.0.1:3001", help="the running Crophecy server")
    ap.add_argument("--out", type=Path, default=Path("site"), help="output directory (emptied first, a .git inside is kept)")
    args = ap.parse_args()
    build(args.base_url, args.out.resolve())


if __name__ == "__main__":
    main()
