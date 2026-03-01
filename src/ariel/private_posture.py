from __future__ import annotations

from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

_DEFAULT_PROTECTED_DESTINATIONS = frozenset({"tag:ariel:443"})


def validate_private_tailnet_posture(
    *,
    serve_state: dict[str, Any],
    policy: dict[str, Any],
    allowed_identities: set[str],
    expected_backend_port: int,
    protected_destinations: set[str] | None = None,
) -> list[str]:
    normalized_allowed_identities = {
        _normalize_identity(identity) for identity in allowed_identities if identity.strip()
    }
    normalized_protected_destinations = {
        _normalize_destination(destination)
        for destination in (protected_destinations or set(_DEFAULT_PROTECTED_DESTINATIONS))
        if destination.strip()
    }

    errors: list[str] = []
    errors.extend(
        _validate_serve_state(
            serve_state=serve_state,
            expected_backend_port=expected_backend_port,
        )
    )
    errors.extend(
        _validate_policy(
            policy=policy,
            allowed_identities=normalized_allowed_identities,
            protected_destinations=normalized_protected_destinations,
        )
    )
    return errors


def _validate_serve_state(*, serve_state: dict[str, Any], expected_backend_port: int) -> list[str]:
    errors: list[str] = []
    if not _has_https_listener(serve_state):
        errors.append("serve state must include an HTTPS listener for private tailnet ingress")

    proxy_targets = _collect_proxy_targets(serve_state)
    if not proxy_targets:
        errors.append("serve state must include at least one proxy target")
        return errors

    for proxy_target in proxy_targets:
        parsed = urlparse(proxy_target)
        host = parsed.hostname
        if host is None:
            errors.append(f"proxy target '{proxy_target}' is invalid")
            continue
        if not _is_loopback(host):
            errors.append(
                f"proxy target '{proxy_target}' must point to a loopback backend for private ingress"
            )
        resolved_port = parsed.port
        if resolved_port is None:
            if parsed.scheme == "https":
                resolved_port = 443
            else:
                resolved_port = 80
        if resolved_port != expected_backend_port:
            errors.append(
                f"proxy target '{proxy_target}' must use backend port {expected_backend_port}"
            )

    if _funnel_is_enabled(serve_state):
        errors.append("funnel must remain disabled to preserve private-only ingress")

    return errors


def _validate_policy(
    *,
    policy: dict[str, Any],
    allowed_identities: set[str],
    protected_destinations: set[str],
) -> list[str]:
    errors: list[str] = []
    acl_entries = policy.get("acls")
    if not isinstance(acl_entries, list) or not acl_entries:
        return ["tailnet policy must define non-empty acls for explicit allowlist enforcement"]
    if not allowed_identities:
        return ["at least one explicit allowlisted identity is required"]
    if not protected_destinations:
        return ["at least one protected destination is required"]

    observed_sources: set[str] = set()
    covered_surface = False
    for entry in acl_entries:
        if not isinstance(entry, dict):
            errors.append("tailnet policy acl entries must be objects")
            continue

        action = entry.get("action")
        if isinstance(action, str) and action.lower() not in {"accept", "allow"}:
            continue

        src = entry.get("src")
        dst = entry.get("dst")
        if not isinstance(src, list) or not isinstance(dst, list):
            errors.append("tailnet policy acl entries must include list src/dst fields")
            continue

        covers_protected_surface = False
        for destination in dst:
            if not isinstance(destination, str):
                errors.append("tailnet policy dst entries must be strings")
                continue
            normalized_destination = _normalize_destination(destination)
            if _is_wildcard_destination(normalized_destination):
                covers_protected_surface = True
                continue
            if normalized_destination in protected_destinations:
                covers_protected_surface = True

        if not covers_protected_surface:
            continue
        covered_surface = True

        if any(_is_wildcard_destination(_normalize_destination(value)) for value in dst if isinstance(value, str)):
            errors.append("tailnet policy destination must not expose wildcard targets")

        for identity in src:
            if not isinstance(identity, str):
                errors.append("tailnet policy src identities must be strings")
                continue
            normalized = _normalize_identity(identity)
            if normalized in {"*", "autogroup:internet"}:
                errors.append(
                    "tailnet policy allowlist must not include wildcard/public identities"
                )
                continue
            observed_sources.add(normalized)
            if normalized not in allowed_identities:
                errors.append(
                    f"tailnet policy identity '{normalized}' is outside the explicit allowlist"
                )

    if not covered_surface:
        errors.append(
            "tailnet policy must include an accept rule for protected destinations: "
            + ", ".join(sorted(protected_destinations))
        )

    if covered_surface:
        missing_identities = sorted(allowed_identities - observed_sources)
        if missing_identities:
            errors.append(
                "tailnet policy is missing allowlist identities: " + ", ".join(missing_identities)
            )
    if covered_surface and not observed_sources:
        errors.append("tailnet policy must include at least one explicit allowlist identity")

    return errors


def _collect_proxy_targets(node: Any) -> list[str]:
    targets: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() == "proxy" and isinstance(value, str):
                targets.append(value)
                continue
            targets.extend(_collect_proxy_targets(value))
    elif isinstance(node, list):
        for item in node:
            targets.extend(_collect_proxy_targets(item))
    return targets


def _has_https_listener(node: Any, *, within_https_key: bool = False) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            nested_within_https_key = within_https_key or ("https" in key.lower())
            if _has_https_listener(value, within_https_key=nested_within_https_key):
                return True
        return False
    if isinstance(node, list):
        return any(_has_https_listener(item, within_https_key=within_https_key) for item in node)
    if not within_https_key:
        return False
    return _is_enabled_value(node)


def _funnel_is_enabled(node: Any, *, within_funnel_key: bool = False) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            nested_within_funnel_key = within_funnel_key or ("funnel" in key.lower())
            if _funnel_is_enabled(value, within_funnel_key=nested_within_funnel_key):
                return True
        return False
    if isinstance(node, list):
        return any(_funnel_is_enabled(item, within_funnel_key=within_funnel_key) for item in node)
    if not within_funnel_key:
        return False
    return _is_enabled_value(node)


def _is_enabled_value(node: Any) -> bool:
    if isinstance(node, bool):
        return node
    if isinstance(node, str):
        return node.strip().lower() in {"true", "1", "yes", "on", "enabled"}
    if isinstance(node, (int, float)):
        return node != 0
    return bool(node)


def _is_wildcard_destination(value: str) -> bool:
    return value in {"*", "*:*"} or value.endswith(":*")


def _normalize_identity(value: str) -> str:
    return value.strip().lower()


def _normalize_destination(value: str) -> str:
    return value.strip().lower()


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
