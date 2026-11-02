"""The committed OpenAPI document of the console API (plan M2, "Interfaces fixed").

``carto-core openapi`` renders it from the application built with in-memory ports and default
settings; ``core/openapi/openapi.v1.json`` is the committed copy the web client is generated
from and the authorization matrix test reads. Every operation carries ``x-carto-permission``
(:data:`carto_core.api.sessions.PERMISSION_EXTENSION`). The rendering is canonical (sorted
keys, two-space indent, trailing newline) so a drift test can compare bytes, and the settings
are built without the environment so the document does not depend on the machine that renders
it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from carto_core.api.app import create_app
from carto_core.api.memory_ports import memory_ports
from carto_core.settings import ApiSettings, CoreSettings
from carto_schema.api import API_VERSION

__all__ = ["OPENAPI_FILE", "build_document", "render_openapi"]

OPENAPI_FILE: Final = Path(__file__).resolve().parents[2] / "openapi" / "openapi.v1.json"


def build_document() -> dict[str, Any]:
    settings = CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True))
    app = create_app(settings, memory_ports())
    document = app.openapi()
    document["info"]["version"] = API_VERSION
    document["servers"] = [{"url": "/"}]
    return document


def render_openapi() -> str:
    """The OpenAPI 3.1 document as sorted, two-space-indented JSON with a trailing newline."""
    return json.dumps(build_document(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
