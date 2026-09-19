"""Non-domain smoke endpoint for local operational checks."""

from flask import Blueprint, Response


health_blueprint = Blueprint("health", __name__)


@health_blueprint.get("/health")
def health() -> Response:
    """Return a minimal response without exposing application or portfolio data."""

    return Response("ok\n", mimetype="text/plain")
