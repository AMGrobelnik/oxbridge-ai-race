"""Offline tests for the Pay.sh command (no `pay` CLI or network needed)."""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.commands import paysh


def field(md: str, key: str) -> str:
    match = re.search(rf'^{key}: "?(.*?)"?$', md, re.MULTILINE)
    return match.group(1) if match else ""


class PayshTests(unittest.TestCase):
    def test_reads_devnet_wallet(self) -> None:
        out = "devnet:\n● default [devnet 2sQzRvwDikhtMLaicsUEm372c6CNErMQpiZYZEUjuyS4]\nlocalnet:\n● default [localnet B2mWBJMBWzG9bEfbEEv1qohTotpqK5BSUEcmFnjCwMp6]"
        self.assertEqual(paysh.devnet_wallet_from(out), "2sQzRvwDikhtMLaicsUEm372c6CNErMQpiZYZEUjuyS4")
        self.assertIsNone(paysh.devnet_wallet_from("localnet:\n● default [localnet B2mWBJMBWzG9bEfbEEv1qohTotpqK5BSUEcmFnjCwMp6]"))

    def test_slug_follows_catalog_rules(self) -> None:
        self.assertEqual(paysh.slug("Team Rocket!"), "team-rocket")
        self.assertEqual(paysh.slug("--My__Agent--"), "my-agent")

    def test_listing_fits_catalog_limits(self) -> None:
        md = paysh.listing_markdown("a-very-long-project-name-for-testing", "https://api.example.com", 0.01)
        self.assertEqual(field(md, "name"), "a-very-long-project-name-for-testing")
        self.assertTrue(64 <= len(field(md, "description")) <= 255)
        self.assertTrue(32 <= len(field(md, "use_case")) <= 255)
        self.assertRegex(md, r"(?m)^ {4}path: premium-data$")
        self.assertRegex(md, r"(?m)price_usd: 0\.01$")

    def test_explains_when_pay_is_missing(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError), mock.patch("builtins.print") as printed:
            self.assertIsNone(paysh.pay(["--version"]))
        self.assertIn("brew install pay", printed.call_args.args[0])

    def test_listing_writes_pay_md_in_the_catalog_layout(self) -> None:
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp, "Fact Agent")
            project.mkdir()
            os.chdir(project)
            try:
                with mock.patch.object(paysh, "pay", return_value=None), mock.patch("builtins.print"):
                    paysh.listing("Team Rocket")
                written = Path("pay-listing/providers/team-rocket/fact-agent/PAY.md").read_text(encoding="utf-8")
            finally:
                os.chdir(cwd)
        self.assertIn("name: fact-agent", written)


if __name__ == "__main__":
    unittest.main()
