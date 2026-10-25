"""Offline tests for the race adapter's protocol handling. Run: python -m unittest discover -s tests -v

They check the envelope, the token and the nonce, not your AI logic: the race verifier does that.
"""

import asyncio
import unittest
import json
from typing import Any

import httpx

from agent.race import handlers as h
from agent.race.adapter import handle_health, handle_task
from agent.race.memory import Memory
from agent.race.protocol import TaskError

ENV = {"AGENT_STUDIO_VERIFIER_TOKEN": "test-token-0123456789"}
AUTH = f"Bearer {ENV['AGENT_STUDIO_VERIFIER_TOKEN']}"


def request(task: dict[str, Any]) -> dict[str, Any]:
    return {"protocol": "asrp/1", "nonce": "nonce-0123456789abcdef", "attempt": {"id": "a", "layer": "l", "number": 1}, "task": task}


class RaceAdapterTest(unittest.TestCase):
    def test_health_needs_the_token(self) -> None:
        self.assertEqual(handle_health(None, {})[0], 503)
        self.assertEqual(handle_health("Bearer wrong", ENV)[0], 401)
        status, body = handle_health(AUTH, ENV)
        self.assertEqual(status, 200)
        self.assertEqual((body["protocol"], body["status"]), ("asrp/1", "ready"))

    def test_rejects_unknown_callers_and_malformed_requests(self) -> None:
        status, body = asyncio.run(handle_task("Bearer nope", request({"type": "respond", "input": "hi"}), ENV))
        self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
        status, body = asyncio.run(handle_task(AUTH, {"nonce": "n" * 16, "task": {"type": "dance"}}, ENV))
        self.assertEqual((status, body["nonce"]), (400, "n" * 16))

    def test_wraps_answers_with_the_echoed_nonce(self) -> None:
        original = h.HANDLERS["respond"]

        async def echo(task: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
            return {"text": f"echo: {task['input']}"}

        h.HANDLERS["respond"] = echo
        try:
            status, body = asyncio.run(handle_task(AUTH, request({"type": "respond", "input": "hi"}), ENV))
            self.assertEqual(status, 200)
            self.assertEqual(body, {"protocol": "asrp/1", "nonce": "nonce-0123456789abcdef", "status": "ok", "output": {"text": "echo: hi"}})
            self.assertTrue(h.is_built("respond"))
        finally:
            h.HANDLERS["respond"] = original

    def test_task_error_becomes_the_matching_protocol_error(self) -> None:
        original = h.HANDLERS["extract"]

        async def bad(_task: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
            raise TaskError("invalid_input", "no schema")

        h.HANDLERS["extract"] = bad
        try:
            status, body = asyncio.run(handle_task(AUTH, request({"type": "extract", "input": ""}), ENV))
            self.assertEqual((status, body["error"]), (400, {"code": "invalid_input", "message": "no schema"}))
        finally:
            h.HANDLERS["extract"] = original

    def test_unbuilt_task_types_say_so(self) -> None:
        original = h.HANDLERS["converse"]
        h.HANDLERS["converse"] = h.not_yet("converse")
        try:
            status, body = asyncio.run(handle_task(AUTH, request({"type": "converse", "input": "hi", "sessionId": "s"}), ENV))
            self.assertEqual(status, 500)
            self.assertIn('doesn\'t handle "converse" tasks yet', body["error"]["message"])
            self.assertFalse(h.is_built("converse"))
        finally:
            h.HANDLERS["converse"] = original


if __name__ == "__main__":
    unittest.main()


class MemoryTest(unittest.TestCase):
    def test_keeps_sessions_apart_in_process(self) -> None:
        m = Memory({})
        self.assertFalse(m.persistent)
        asyncio.run(m.append("a", {"role": "user", "text": "codename is fox-12"}))
        asyncio.run(m.append("b", {"role": "user", "text": "something else"}))
        self.assertEqual(asyncio.run(m.history("a")), [{"role": "user", "text": "codename is fox-12"}])
        self.assertEqual(asyncio.run(m.history("missing")), [])

    def test_uses_upstash_when_configured(self) -> None:
        calls: list[tuple[str, Any]] = []
        store: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.headers["authorization"], "Bearer tok")
            body = json.loads(req.content)
            calls.append((str(req.url), body))
            if str(req.url).endswith("/pipeline"):
                store.extend(body[0][2:])
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"result": store})

        m = Memory({"KV_REST_API_URL": "https://eu1.upstash.io/", "KV_REST_API_TOKEN": "tok"}, httpx.MockTransport(handler))
        self.assertTrue(m.persistent)
        asyncio.run(m.append("s1", {"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}))
        self.assertEqual(asyncio.run(m.history("s1")), [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}])
        self.assertEqual(calls[0][0], "https://eu1.upstash.io/pipeline")
        self.assertEqual([c[0] for c in calls[0][1]], ["RPUSH", "LTRIM", "EXPIRE"])
        self.assertEqual(calls[1][1], ["LRANGE", "race:memory:s1", 0, -1])
