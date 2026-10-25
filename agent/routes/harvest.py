"""Harvest JSON API under /harvest/api/* incl. the external agent API (SPEC.md section 11, addendum R1). Owner: market-engine."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Skeleton: market-engine adds every /harvest/api/* route here."""
