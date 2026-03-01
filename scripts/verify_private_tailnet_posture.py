#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ariel.private_posture import validate_private_tailnet_posture


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"{label} file not found: {path}"
        raise RuntimeError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"{label} file is not valid JSON: {path} ({exc})"
        raise RuntimeError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"{label} file must decode to a JSON object: {path}"
        raise RuntimeError(msg)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Ariel is private-only behind Tailscale ingress controls."
    )
    parser.add_argument(
        "--serve-status-json",
        type=Path,
        required=True,
        help="Path to tailscale serve status JSON output.",
    )
    parser.add_argument(
        "--policy-json",
        type=Path,
        required=True,
        help="Path to tailnet ACL policy JSON file.",
    )
    parser.add_argument(
        "--allowed-identity",
        action="append",
        default=[],
        help="Explicitly allowlisted tailnet identity (repeat for each identity/device tag).",
    )
    parser.add_argument(
        "--backend-port",
        type=int,
        default=8000,
        help="Expected localhost backend port Ariel listens on.",
    )
    parser.add_argument(
        "--protected-destination",
        action="append",
        default=["tag:ariel:443"],
        help=(
            "Destination selector representing Ariel's protected ingress surface "
            "(repeatable, defaults to tag:ariel:443)."
        ),
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.allowed_identity:
        parser.error("at least one --allowed-identity is required")
    if not args.protected_destination:
        parser.error("at least one --protected-destination is required")

    try:
        serve_state = _load_json(args.serve_status_json, label="serve-status")
        policy = _load_json(args.policy_json, label="policy")
    except RuntimeError as exc:
        print(f"private posture check failed: {exc}")
        return 2

    errors = validate_private_tailnet_posture(
        serve_state=serve_state,
        policy=policy,
        allowed_identities=set(args.allowed_identity),
        expected_backend_port=args.backend_port,
        protected_destinations=set(args.protected_destination),
    )
    if errors:
        print("private posture check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("private posture check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
