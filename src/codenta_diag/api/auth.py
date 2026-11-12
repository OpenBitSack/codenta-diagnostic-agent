"""API-key auth for the prototype.

Production design (not implemented here): OIDC/JWT from the company IdP via an API gateway, with
role + practice claims; service-to-service auth via IAM roles. The `Principal` abstraction below is
what route handlers depend on, so swapping the credential mechanism does not touch handlers.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request, status

from ..intake import Principal


def principal_from_key(request: Request, key: str | None) -> Principal | None:
    if not key:
        return None
    spec = request.app.state.settings.api_keys.get(key)
    if spec is None:
        return None
    return Principal(principal=spec["principal"], role=spec["role"], practices=list(spec["practices"]))


def get_principal(request: Request) -> Principal:
    p = principal_from_key(request, request.headers.get("X-API-Key"))
    if p is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-API-Key")
    return p


def require(*roles: str) -> Callable[[Principal], Principal]:
    def dep(p: Principal = Depends(get_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role {p.role!r} not permitted")
        return p
    return dep
