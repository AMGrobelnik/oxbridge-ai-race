"""Offline checks for the base agent. Run: python -m unittest discover -s tests -v"""

import asyncio
import json
import unittest

from starlette.requests import Request

from agent.__main__ import build_parser
from agent.modules import modules
from agent.server import app, home


def get_home(accept: str) -> tuple[str, bytes]:
    """Calls GET / with an Accept header and returns (content type, body)."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"accept", accept.encode())],
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
    }
    response = asyncio.run(home(Request(scope)))
    return response.media_type or "", bytes(response.body)


class AgentTest(unittest.TestCase):
    def test_home_lists_the_generated_modules_as_json(self) -> None:
        media_type, body = get_home("*/*")
        self.assertEqual(media_type, "application/json")
        self.assertEqual(json.loads(body)["modules"], modules)

    def test_home_is_a_page_for_browsers(self) -> None:
        media_type, body = get_home("text/html,application/xhtml+xml,*/*;q=0.8")
        self.assertEqual(media_type, "text/html")
        self.assertIn(b"is running", body)

    def test_every_route_file_loaded(self) -> None:
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/", paths)

    def test_command_line_parses(self) -> None:
        args = build_parser().parse_args(["serve"])
        self.assertEqual(args.command, "serve")


if __name__ == "__main__":
    unittest.main()
