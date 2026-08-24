"""Diagnostic: verify the configured model IDs exist on the configured endpoint.

Run as ``python -m scripts.ai.check_models``.

Prints only model IDs and a pass/fail verdict. The API key is never printed and
the endpoint listing is not written to an artifact.

Exit codes: 0 both models available, 1 a configured model is missing,
2 the endpoint could not be queried.
"""
from __future__ import annotations

import os
import sys
from typing import Mapping

from ..common.errors import AIError
from .client import base_url, list_models, metadata_model, review_model


def check(available: list[str], wanted: Mapping[str, str]) -> dict[str, bool]:
    catalog = set(available)
    return {label: model in catalog for label, model in wanted.items()}


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    wanted = {"AI_METADATA_MODEL": metadata_model(env), "AI_REVIEW_MODEL": review_model(env)}
    print(f"Endpoint: {base_url(env)}")
    for label, model in wanted.items():
        print(f"Configured {label}: {model}")

    try:
        available = list_models(env=env)
    except AIError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    print(f"Endpoint advertises {len(available)} models.")
    verdict = check(available, wanted)
    missing = [label for label, ok in verdict.items() if not ok]
    for label, ok in verdict.items():
        print(f"{label}: {'AVAILABLE' if ok else 'NOT FOUND'}")
    if missing:
        print(
            "::error::Configured model IDs were not found on this endpoint: "
            + ", ".join(missing)
            + ". Override them with the AI_METADATA_MODEL / AI_REVIEW_MODEL "
            "repository variables, or verify entitlement for these models.",
            file=sys.stderr,
        )
        # Print the closest candidates to make the override obvious.
        for label in missing:
            prefix = wanted[label].split("/", 1)[0]
            related = [m for m in available if m.startswith(prefix)][:10]
            if related:
                print(f"Models under '{prefix}/': " + ", ".join(related))
        return 1
    print("All configured models are available.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
