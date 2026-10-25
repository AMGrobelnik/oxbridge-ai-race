"""Conversation memory for the Memory and Context layer: the turns of one session, kept apart from
every other session.

Where it's kept matters. On serverless hosting (Vercel, Cloud Run…) each request can reach a
different instance, so a variable in this file forgets between turns even though it works on your
laptop. Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN (a free Upstash Redis database;
Vercel's Upstash integration sets KV_REST_API_URL and KV_REST_API_TOKEN, which also work) and
history is stored there. Without them it falls back to this process's memory, which is fine
locally and on a single always-on server.

    history = await memory.history(task.get("sessionId"))   # [{"role": "user", "text": "…"}, …]
    … answer task["input"] using history …
    await memory.append(task.get("sessionId"), {"role": "user", "text": str(task["input"])}, {"role": "assistant", "text": answer})
"""

import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

import httpx

MAX_TURNS = 100
TTL_SECONDS = 2 * 24 * 3600


class Turn(TypedDict):
    role: Literal["user", "assistant"]
    text: str


class Memory:
    def __init__(self, env: Mapping[str, str] | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        env = os.environ if env is None else env
        self._url = (env.get("UPSTASH_REDIS_REST_URL") or env.get("KV_REST_API_URL") or "").rstrip("/")
        self._token = env.get("UPSTASH_REDIS_REST_TOKEN") or env.get("KV_REST_API_TOKEN") or ""
        self._transport = transport
        self._local: dict[str, list[Turn]] = {}
        self._warned = False
        #: True when history is stored in Upstash Redis (survives across serverless instances).
        self.persistent = bool(self._url and self._token)

    async def _redis(self, path: str, body: Any) -> Any:
        async with httpx.AsyncClient(timeout=5, transport=self._transport) as client:
            res = await client.post(f"{self._url}{path}", json=body, headers={"authorization": f"Bearer {self._token}"})
        res.raise_for_status()
        return res.json()

    async def history(self, session_id: str | None) -> list[Turn]:
        """Earlier turns of this session, oldest first."""
        if not session_id:
            return []
        if not self.persistent:
            if not self._warned and (os.environ.get("VERCEL") or os.environ.get("K_SERVICE")):
                self._warned = True
                logging.warning("[race] Memory is in-process: on serverless hosting it is lost between turns. Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN (see agent/race/memory.py).")
            return list(self._local.get(session_id, []))
        result = (await self._redis("", ["LRANGE", f"race:memory:{session_id}", 0, -1])).get("result") or []
        return [json.loads(s) for s in result]

    async def append(self, session_id: str | None, *turns: Turn) -> None:
        """Adds turns to this session's history (keeps the latest 100; expires after two days)."""
        if not session_id or not turns:
            return
        if not self.persistent:
            self._local[session_id] = [*self._local.get(session_id, []), *turns][-MAX_TURNS:]
            return
        key = f"race:memory:{session_id}"
        await self._redis("/pipeline", [["RPUSH", key, *[json.dumps(t) for t in turns]], ["LTRIM", key, -MAX_TURNS, -1], ["EXPIRE", key, TTL_SECONDS]])


#: The app's memory, configured from the environment.
memory = Memory()
