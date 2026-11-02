"""RFC 7807 problems for the console API (spec 12), on top of the ingest-api's
:class:`~carto_core.ingest.app.Problem`.

One shape for every error path: ``Problem`` raised by handlers, :class:`AuthorizationError`
from :func:`carto_core.api.auth.require` (401 and 403), FastAPI's validation errors (422 with
locations, messages and types, never the input), Starlette's routing errors (404, 405) and the
catch-all (500, logged by type only). ``instance`` is the request id, so a user can quote the
problem to an operator who finds the log line without either seeing the body (spec 14.12). A
problem body never carries a header value, a query value or a body fragment of the request.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from carto_common.logging import get_logger
from carto_core.api.auth import AuthorizationError
from carto_core.ingest.app import PROBLEM_CONTENT_TYPE, Problem

__all__ = ["PROBLEM_CONTENT_TYPE", "Problem", "install_problem_handlers", "problem_response"]

log = get_logger(component="api")

_HTTP_TITLES = {404: "not found", 405: "method not allowed"}


def problem_response(request: Request, problem: Problem) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
        "instance": f"urn:carto:request:{request_id}" if request_id else None,
    }
    if problem.errors is not None:
        body["errors"] = problem.errors
    headers = {"Cache-Control": "no-store", **(problem.headers or {})}
    return JSONResponse(
        body, status_code=problem.status, media_type=PROBLEM_CONTENT_TYPE, headers=headers
    )


def _safe_location(error: dict[str, Any]) -> list[Any]:
    """The location without a key the client invented (``extra_forbidden`` puts the unknown key
    in ``loc``, which would echo request content)."""
    loc = list(error.get("loc", ()))
    if error.get("type") == "extra_forbidden" and loc:
        loc[-1] = "<unknown key>"
    return loc


def validation_problem(exc: RequestValidationError) -> Problem:
    errors = [
        {"loc": _safe_location(error), "msg": str(error.get("msg", "")), "type": error["type"]}
        for error in exc.errors()
    ]
    if errors and all(error["type"] == "json_invalid" for error in errors):
        return Problem(400, "bad request", "body is not valid JSON")
    return Problem(422, "request failed validation", "see errors", errors=errors)


def install_problem_handlers(app: FastAPI, *, on_problem: Any = None) -> None:
    """Register the handlers; ``on_problem(status)`` is called for every problem (metrics)."""

    def count(status: int) -> None:
        if on_problem is not None:
            on_problem(status)

    @app.exception_handler(Problem)
    async def on_problem_raised(request: Request, exc: Problem) -> Response:
        count(exc.status)
        return problem_response(request, exc)

    @app.exception_handler(AuthorizationError)
    async def on_authorization(request: Request, exc: AuthorizationError) -> Response:
        count(exc.status)
        title = "authentication required" if exc.status == 401 else "forbidden"
        return problem_response(request, Problem(exc.status, title, exc.detail))

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        count(exc.status_code)
        title = _HTTP_TITLES.get(exc.status_code, "request refused")
        headers = dict(exc.headers) if exc.headers else None
        return problem_response(request, Problem(exc.status_code, title, headers=headers))

    @app.exception_handler(RequestValidationError)
    async def on_request_validation(request: Request, exc: RequestValidationError) -> Response:
        problem = validation_problem(exc)
        count(problem.status)
        return problem_response(request, problem)

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception) -> Response:
        count(500)
        log.error("unhandled error", error=type(exc).__name__, path=request.url.path)
        return problem_response(request, Problem(500, "internal error"))
