from __future__ import annotations

from typing import Any

from ariel.app import create_app


def create_test_app(
    *,
    database_url: str,
    model_adapter: Any | None = None,
    sandbox: Any | None = None,
) -> Any:
    return create_app(
        database_url=database_url,
        model_adapter=model_adapter,
        sandbox=sandbox,
    )
