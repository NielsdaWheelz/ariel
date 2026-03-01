from __future__ import annotations

from ariel.private_posture import validate_private_tailnet_posture


def test_validate_private_tailnet_posture_accepts_loopback_proxy_and_allowlist() -> None:
    serve_state = {
        "TCP": {
            "443": {
                "HTTPS": True,
                "AllowFunnel": {"443": False},
                "Web": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}},
            }
        }
    }
    policy = {
        "acls": [
            {
                "action": "accept",
                "src": ["user:alice@example.com", "tag:alice-phone"],
                "dst": ["tag:ariel:443"],
            }
        ]
    }

    errors = validate_private_tailnet_posture(
        serve_state=serve_state,
        policy=policy,
        allowed_identities={"user:alice@example.com", "tag:alice-phone"},
        expected_backend_port=8000,
    )

    assert errors == []


def test_validate_private_tailnet_posture_rejects_public_or_broad_exposure() -> None:
    serve_state = {
        "TCP": {
            "443": {
                "HTTPS": True,
                "AllowFunnel": {"443": True},
                "Web": {"Handlers": {"/": {"Proxy": "http://0.0.0.0:8000"}}},
            }
        }
    }
    policy = {
        "acls": [
            {
                "action": "accept",
                "src": ["*", "user:bob@example.com"],
                "dst": ["tag:ariel:443"],
            }
        ]
    }

    errors = validate_private_tailnet_posture(
        serve_state=serve_state,
        policy=policy,
        allowed_identities={"user:alice@example.com"},
        expected_backend_port=8000,
    )

    assert any("funnel" in error.lower() for error in errors)
    assert any("loopback" in error.lower() for error in errors)
    assert any("allowlist" in error.lower() for error in errors)


def test_validate_private_tailnet_posture_requires_https_listener() -> None:
    serve_state = {
        "TCP": {
            "443": {
                "AllowFunnel": {"443": False},
                "Web": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}},
            }
        }
    }
    policy = {
        "acls": [
            {
                "action": "accept",
                "src": ["user:alice@example.com"],
                "dst": ["tag:ariel:443"],
            }
        ]
    }

    errors = validate_private_tailnet_posture(
        serve_state=serve_state,
        policy=policy,
        allowed_identities={"user:alice@example.com"},
        expected_backend_port=8000,
    )

    assert any("https listener" in error.lower() for error in errors)


def test_validate_private_tailnet_posture_ignores_unrelated_acl_entries() -> None:
    serve_state = {
        "TCP": {
            "443": {
                "HTTPS": True,
                "AllowFunnel": {"443": False},
                "Web": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}},
            }
        }
    }
    policy = {
        "acls": [
            {
                "action": "accept",
                "src": ["user:alice@example.com"],
                "dst": ["tag:ariel:443"],
            },
            {
                "action": "accept",
                "src": ["user:bob@example.com"],
                "dst": ["tag:other-service:443"],
            },
        ]
    }

    errors = validate_private_tailnet_posture(
        serve_state=serve_state,
        policy=policy,
        allowed_identities={"user:alice@example.com"},
        expected_backend_port=8000,
    )

    assert errors == []
