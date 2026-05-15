from __future__ import annotations

from pathlib import PurePosixPath
import shlex


def denylisted_terminal_command(cwd: str, command: str) -> str | None:
    text = f"{cwd}\n{command}".lower()
    for token in (
        "/proc/self/environ",
        "$home/.",
        "${home}/.",
        "$pwd/.env",
        "${pwd}/.env",
        "~/.env",
        "~/.ssh",
        "~/.aws",
        "~/.npmrc",
        "~/.netrc",
        "~/.git-credentials",
        "~/.config/gh",
        "~/.config/gcloud",
        "~/.docker",
        "~/.kube",
        "~/.gnupg",
        "/.env",
        "/.ssh",
        "/.aws",
        "/.config/gh",
        "/.config/gcloud",
        "/.docker",
        "/.kube",
        "/.gnupg",
        "/credentials",
        "/credential",
        "/secrets",
        "/secret",
        "id_rsa",
        "id_ed25519",
    ):
        if token in text:
            return "terminal_command_denied_secret_path"

    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()

    if words and words[0].lower() == "rg":
        for word in words[1:]:
            if word.lower() in {"--hidden", "--no-ignore", "-u", "-uu", "-uuu"}:
                return "terminal_command_denied_secret_path"

    secret_path_parts = {
        ".ssh",
        ".aws",
        ".config/gh",
        ".config/gcloud",
        ".docker",
        ".kube",
        ".gnupg",
    }
    for word in words:
        normalized = word.strip().strip("<>").strip("'\"").lower()
        if normalized.startswith("$pwd/"):
            normalized = cwd.lower().rstrip("/") + normalized.removeprefix("$pwd")
        if normalized.startswith("${pwd}/"):
            normalized = cwd.lower().rstrip("/") + normalized.removeprefix("${pwd}")
        deglobbed = normalized.translate(str.maketrans("", "", "[]{}?*"))
        for candidate in {normalized, deglobbed}:
            if ":.env" in candidate or candidate.endswith(":.*"):
                return "terminal_command_denied_secret_path"
            path_parts = [part for part in PurePosixPath(candidate).parts if part not in {"/", "."}]
            basename = path_parts[-1] if path_parts else candidate
            if basename == ".env" or basename.startswith(".env."):
                return "terminal_command_denied_secret_path"
            if basename in {".npmrc", ".netrc", ".git-credentials"}:
                return "terminal_command_denied_secret_path"
            if candidate in {
                ".*",
                ".env",
                ".npmrc",
                ".netrc",
                ".git-credentials",
            }:
                return "terminal_command_denied_secret_path"
            if candidate.startswith((".env.", ".config/gh", ".docker", ".kube", ".gnupg")):
                return "terminal_command_denied_secret_path"
            joined_parts = "/".join(path_parts)
            if any(part in secret_path_parts for part in path_parts):
                return "terminal_command_denied_secret_path"
            if any(secret_part in joined_parts for secret_part in secret_path_parts):
                return "terminal_command_denied_secret_path"

    return None
