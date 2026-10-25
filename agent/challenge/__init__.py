"""Beat the Seal: a public challenge on the Sealed `strategy` plug-in (GET /challenge).

game.py scores a visitor's strategy on the visible dev windows and on the sealed holdout plus
stress scenarios; ledger.py keeps every submission in a hash chain whose head is anchored on
Solana devnet in batches; ratelimit.py is the per-IP limiter the routes use.
"""
