"""HTTP API and demo page for Sealed under /sealed (SPEC.md section 9). Owner: W4.

Every error is JSON {"error": ...}. A W1/W2 function that is still a stub (NotImplementedError)
gives 501 instead of a 500, so the page and the rest of the app keep working.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from agent.payments import cosign
from agent.sealed import audit, chain, harness, prereg, store
from agent.sealed.environment import UnknownEnvironmentError, list_environments, load_environment
from agent.sealed.records import RUN_ID_PATTERN

STATIC_DIR = Path(__file__).resolve().parent.parent / "sealed" / "static"
PROPOSERS = ("gemini", "scripted")
MAX_ITERATIONS = 12
DEFAULT_DEMO_ENV = "strategy"
GUARDRAIL_DEMO_USDC = 2.5
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _not_ready(err: NotImplementedError) -> JSONResponse:
    return _error(f"Not implemented yet: {err or 'this Sealed module is still a stub'}", 501)


def _bad_run_id(run_id: str) -> JSONResponse | None:
    if _RUN_ID_RE.fullmatch(run_id):
        return None
    return _error(f"Invalid run id: expected {RUN_ID_PATTERN}", 400)


def _load_run(run_id: str) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """The stored record, or the error response to return instead."""
    if (bad := _bad_run_id(run_id)) is not None:
        return None, bad
    try:
        return dict(store.RunStore().load(run_id)), None
    except store.RunNotFoundError:
        return None, _error(f"Unknown run {run_id!r}", 404)
    except NotImplementedError as err:
        return None, _not_ready(err)
    except (OSError, ValueError) as err:
        return None, _error(f"Run {run_id!r} could not be read: {type(err).__name__}", 500)


def _load_demo(env: str = DEFAULT_DEMO_ENV) -> dict[str, Any]:
    try:
        return dict(store.load_demo(env))
    except NotImplementedError:
        return json.loads(store.SAMPLE_RUN.read_text(encoding="utf-8"))


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


async def _onchain_memo(receipt: Any) -> str | None:
    """The memo on chain for a confirmed receipt, else None (pending or unreadable)."""
    if not isinstance(receipt, dict) or receipt.get("status") != "confirmed" or not receipt.get("signature"):
        return None
    return await chain.fetch_memo(str(receipt["signature"]))


def register(app: FastAPI) -> None:
    app.mount("/sealed/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="sealed-static")

    @app.get("/sealed", response_model=None)
    async def sealed_page() -> FileResponse | JSONResponse:
        index = STATIC_DIR / "index.html"
        if not index.exists():
            return _error("The Sealed page is not built yet.", 501)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    @app.get("/sealed/api/envs")
    async def sealed_envs() -> JSONResponse:
        views = []
        for name in list_environments():
            try:
                views.append(load_environment(name).public_view())
            except (ImportError, AttributeError, KeyError, NotImplementedError) as err:
                views.append({"name": name, "error": f"plug-in not loadable: {type(err).__name__}"})
        return JSONResponse(views)

    @app.get("/sealed/api/demo")
    async def sealed_demo(env: str = DEFAULT_DEMO_ENV) -> JSONResponse:
        if env not in list_environments():
            return _error(f"Unknown env: expected one of {', '.join(list_environments()) or 'none'}", 400)
        try:
            return JSONResponse(_load_demo(env))
        except (OSError, ValueError) as err:
            return _error(f"The demo run could not be read: {type(err).__name__}", 500)

    @app.get("/sealed/api/runs")
    async def sealed_runs() -> JSONResponse:
        try:
            return JSONResponse(store.RunStore().list_summaries())
        except NotImplementedError as err:
            return _not_ready(err)

    @app.post("/sealed/api/runs")
    async def sealed_start_run(request: Request) -> JSONResponse:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return _error('Send a JSON object: {"env": "epidemic", "iterations": 8, "proposer": "gemini", "anchor": true}', 400)
        env = body.get("env", "epidemic")
        if not isinstance(env, str) or env not in list_environments():
            return _error(f"Unknown env: expected one of {', '.join(list_environments()) or 'none'}", 400)
        proposer = body.get("proposer", "gemini")
        if proposer not in PROPOSERS:
            return _error(f"Unknown proposer: expected one of {', '.join(PROPOSERS)}", 400)
        iterations = body.get("iterations", 8)
        if isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= MAX_ITERATIONS:
            return _error(f"iterations must be an integer from 1 to {MAX_ITERATIONS}", 400)
        anchor = body.get("anchor", True)
        if not isinstance(anchor, bool):
            return _error("anchor must be true or false", 400)
        try:
            run_id = harness.start_background_run(env, iterations=iterations, proposer=proposer, anchor=anchor)
        except NotImplementedError as err:
            return _not_ready(err)
        except (UnknownEnvironmentError, ValueError) as err:
            return _error(str(err), 400)
        return JSONResponse({"run_id": run_id}, status_code=202)

    @app.get("/sealed/api/runs/{run_id}")
    async def sealed_run(run_id: str) -> JSONResponse:
        record, err = _load_run(run_id)
        return err if record is None else JSONResponse(record)

    @app.get("/sealed/api/runs/{run_id}/bundle", response_model=None)
    async def sealed_bundle(run_id: str) -> Response:
        record, err = _load_run(run_id)
        if record is None:
            return err  # type: ignore[return-value]
        body = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
        headers = {"Content-Disposition": f'attachment; filename="sealed-{run_id}.json"'}
        return Response(body, media_type="application/json", headers=headers)

    @app.post("/sealed/api/runs/{run_id}/anchor")
    async def sealed_anchor(run_id: str) -> JSONResponse:
        record, err = _load_run(run_id)
        if record is None:
            return err  # type: ignore[return-value]
        try:
            updated = await chain.anchor_pending(record)  # type: ignore[arg-type]
            store.RunStore().save(updated)
        except NotImplementedError as exc:
            return _not_ready(exc)
        return JSONResponse(updated)

    @app.post("/sealed/api/verify")
    async def sealed_verify(request: Request) -> JSONResponse:
        body = await _json_body(request)
        record = body.get("record") if isinstance(body, dict) else None
        if not isinstance(record, dict):
            return _error('Send {"record": <RunRecord>} (the downloaded bundle).', 400)
        env_name = record.get("env")
        if not isinstance(env_name, str):
            return _error("The bundle has no env field.", 400)
        try:
            env = load_environment(env_name)
        except UnknownEnvironmentError:
            return _error(f"Unknown env {env_name!r} in the bundle.", 400)
        prereg_part = record.get("preregistration")
        reveal_part = record.get("reveal")
        try:
            commit_memo, reveal_memo = await asyncio.gather(
                _onchain_memo(prereg_part.get("chain") if isinstance(prereg_part, dict) else None),
                _onchain_memo(reveal_part.get("chain") if isinstance(reveal_part, dict) else None),
            )
            round_memos = {i: m for i, m in (await audit.fetch_round_memos(record)).items() if m is not None}
            extra = {"round_memos_onchain": round_memos} if round_memos else {}
            report = await asyncio.to_thread(prereg.verify_bundle, record, env, commit_memo_onchain=commit_memo, reveal_memo_onchain=reveal_memo, **extra)  # type: ignore[arg-type]
        except NotImplementedError as err:
            return _not_ready(err)
        return JSONResponse(report)

    @app.post("/sealed/api/guardrail/demo")
    async def sealed_guardrail_demo(request: Request) -> JSONResponse:
        """Queues the demo's over-cap payment (fund the selected candidate) for human co-sign.

        Idempotent: an open approval for the same run and iteration is returned instead of a new one.
        """
        body = await _json_body(request)
        body = body if isinstance(body, dict) else {}
        run_id, iteration, env = body.get("run_id"), body.get("iteration"), body.get("env")
        if not isinstance(run_id, str) or _bad_run_id(run_id) is not None:
            return _error(f"run_id must match {RUN_ID_PATTERN}", 400)
        if isinstance(iteration, bool) or not isinstance(iteration, int) or not 0 <= iteration <= 99:
            return _error("iteration must be an integer from 0 to 99", 400)
        if not isinstance(env, str) or env not in list_environments():
            return _error(f"Unknown env: expected one of {', '.join(list_environments()) or 'none'}", 400)
        memo = f"sealed:v1:deploy:{run_id}:{iteration}"
        for approval in cosign.list_approvals("open"):
            if approval.get("memo") == memo:
                return JSONResponse({"capUsd": cosign.cap_usd(), "approval": approval})
        to = os.environ.get("SEALED_ANCHOR_TO", "").strip()
        if not to:
            from agent.payments import transfer
            from agent.payments.config import PaymentsConfigError

            try:
                to = str(transfer.load_agent_keypair().pubkey())
            except PaymentsConfigError:
                return _error("The agent wallet is not configured (AGENT_PRIVATE_KEY in .env).", 503)
        reason = f"Sealed agent asks to fund the selected {env} candidate #{iteration}"
        approval = cosign.queue(to, GUARDRAIL_DEMO_USDC, memo, reason=reason)
        return JSONResponse({"capUsd": cosign.cap_usd(), "approval": approval})
