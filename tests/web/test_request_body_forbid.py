"""Every request-body model refuses a field it doesn't declare.

A pydantic model's default is to drop an unknown key without a word. On the
dashboard that is how a field renamed in panel JS (or in the model) saves
nothing while the panel reports success. With ``extra="forbid"`` the same
request is a 422 that ``api.js`` shows to the admin. Standards audit row
1.14.

Read from ``app.openapi()``, as the authz sweep is, so a new route is
covered with no registration: a forbid model's schema carries
``"additionalProperties": false``. Nested models are walked too, through
``$ref``, ``items``, ``anyOf``/``oneOf``/``allOf`` and property schemas. A
plain ``dict`` field (free-form options, a pasted card) has no
``properties`` and is not a model, so it is not held to this. Only JSON
bodies are walked: an upload route's multipart body is a model FastAPI
synthesizes from its ``File``/``Form`` parameters, not one of ours.

Before the flip, every panel's payloads were checked against their models,
and no caller sent an undeclared field. This walk found ten models an AST
scan of the route files missed (six inheriting from one base), which is why
it reads the app rather than the source. A new model takes one line:
``model_config = ConfigDict(extra="forbid")``.
"""

from __future__ import annotations

import pytest

from web_server.auth import DiscordOAuthAuth
from web_server.server import create_app


@pytest.fixture
def openapi(fake_ctx) -> dict:
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    return create_app(fake_ctx, auth=auth).openapi()


def _body_refs(spec: dict):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            content = (op.get("requestBody") or {}).get("content", {})
            schema = content.get("application/json", {}).get("schema")
            if schema:
                yield f"{method.upper()} {path}", schema


def _loose_models(spec: dict, schema: dict, seen: set[str]) -> list[str]:
    """Names of model schemas reachable from ``schema`` that allow extras."""
    components = spec.get("components", {}).get("schemas", {})
    found: list[str] = []
    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return found
        seen.add(name)
        target = components[name]
        if "properties" in target and target.get("additionalProperties") is not False:
            found.append(name)
        return found + _loose_models(spec, target, seen)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key, []):
            found += _loose_models(spec, sub, seen)
    if isinstance(schema.get("items"), dict):
        found += _loose_models(spec, schema["items"], seen)
    if isinstance(schema.get("additionalProperties"), dict):
        found += _loose_models(spec, schema["additionalProperties"], seen)
    for prop in schema.get("properties", {}).values():
        found += _loose_models(spec, prop, seen)
    return found


def test_the_sweep_sees_the_request_bodies(openapi):
    """An empty walk would pass the contract below vacuously."""
    assert len(list(_body_refs(openapi))) > 100


def test_every_request_body_model_forbids_extra_fields(openapi):
    loose: dict[str, set[str]] = {}
    for route, schema in _body_refs(openapi):
        for name in _loose_models(openapi, schema, set()):
            loose.setdefault(name, set()).add(route)
    assert not loose, (
        "Request-body models that silently drop undeclared fields — add "
        '`model_config = ConfigDict(extra="forbid")`:\n'
        + "\n".join(f"  {n}: {sorted(r)[0]}" for n, r in sorted(loose.items()))
    )


def test_an_undeclared_field_is_refused(open_client):
    """The behaviour the schema stands for, on one real route: the extra key
    is a 422 naming the field, not a 200 that stored nothing."""
    resp = open_client.post(
        "/api/telemetry/panel", json={"panel": "home", "pnael": "x"}
    )
    assert resp.status_code == 422
    assert any("pnael" in e["loc"] for e in resp.json()["detail"])
