"""A Crophecy bot in 20 lines: register, read the markets and the news, fade the most extreme price. Standard library only.
Run: CROPHECY_URL=https://your-host python example_bot.py   (play credits only, no real money)"""
import json
import os
import urllib.request

BASE = os.environ.get("CROPHECY_URL", "http://127.0.0.1:3001").rstrip("/")


def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


me = call("/harvest/api/agents/register", {"name": "Fade Bot", "world_model": "Prices that look extreme are usually wrong: lean against them."})
markets = call("/harvest/api/agents/markets")["markets"]
news = call("/harvest/api/news?limit=1")["items"]
pick = max(markets, key=lambda m: abs(m["p_yes"] - 0.5))
side = "NO" if pick["p_yes"] > 0.5 else "YES"
why = f"fading {pick['p_yes']:.0%}" + (f"; news: {news[0]['title'][:80]}" if news else "")
bet = call("/harvest/api/agents/bet", {"token": me["token"], "qid": pick["qid"], "side": side, "spend": 10, "rationale": why})
print(f"{me['agent_id']} bet {side} on {pick['title']}: price {bet['trade']['price_before']:.3f} -> {bet['trade']['price_after']:.3f}, balance {bet['balance']:.1f}")
