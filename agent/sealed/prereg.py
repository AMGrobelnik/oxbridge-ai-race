"""Pre-registration hashes, memos and bundle verification (SPEC.md section 7). Owner: W2.

Pure functions, no network: chain.py does the devnet side.
"""

import re

from .environment import Environment
from .records import Holdout, Preregistration, RunRecord, Verdict, VerifyReport

MEMO_PREFIX = "sealed:v1"
COMMIT_MEMO_RE = re.compile(r"^sealed:v1:commit:(?P<run_id>[A-Za-z0-9_-]{1,64}):(?P<commitment>[0-9a-f]{64})$")
REVEAL_MEMO_RE = re.compile(
    r"^sealed:v1:reveal:(?P<run_id>[A-Za-z0-9_-]{1,64}):(?P<salt>[0-9a-f]{64}):(?P<result>[0-9a-f]{64})"
    r":(?P<verdict>GENERALISES|OVERFIT):(?P<selected>\d+|none)$"
)


def new_salt() -> str:
    """32 random bytes as 64 lowercase hex characters (secrets.token_hex)."""
    raise NotImplementedError("W2")


def code_digest(env: Environment) -> str:
    """sha256 over name + NUL + bytes + NUL for each of env.code_files(), in order."""
    raise NotImplementedError("W2")


def holdout_commitment(holdout: Holdout, salt: str) -> str:
    """sha256_json({"salt": salt, "holdout": holdout})."""
    raise NotImplementedError("W2")


def commitment_digest(*, run_id: str, env: str, code_sha256: str, dev_seeds: list[int], holdout_commitment: str) -> str:
    """sha256_json({"v": 1, "run_id", "env", "code_sha256", "dev_seeds", "holdout_commitment"})."""
    raise NotImplementedError("W2")


def commit_memo(run_id: str, commitment: str) -> str:
    """sealed:v1:commit:<run_id>:<commitment>"""
    raise NotImplementedError("W2")


def reveal_memo(run_id: str, salt: str, result_sha256: str, verdict: Verdict, selected_iteration: int | None) -> str:
    """sealed:v1:reveal:<run_id>:<salt>:<result_sha256>:<verdict>:<selected or none>"""
    raise NotImplementedError("W2")


def build_preregistration(env: Environment, run_id: str, salt: str, *, sealed_at: str) -> Preregistration:
    """Everything for the commit memo. Its chain receipt starts pending (memo set, no signature);
    the harness replaces it with chain.anchor(memo)."""
    raise NotImplementedError("W2")


def result_digest(record: RunRecord) -> str:
    """sha256_json of the result payload in SPEC.md section 7 (needs record["reveal"]["results"])."""
    raise NotImplementedError("W2")


def verify_bundle(record: RunRecord, env: Environment, *, commit_memo_onchain: str | None, reveal_memo_onchain: str | None) -> VerifyReport:
    """Recomputes code, holdout commitment, commitment and result hash and compares them with the
    recorded values and, when given, the on-chain memos. Never raises on a tampered bundle: it
    returns ok=False with reasons."""
    raise NotImplementedError("W2")
