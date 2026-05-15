from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Any, Literal

from .capability_registry import CapabilityDefinition, PolicyDecision, get_capability
from .terminal_safety import denylisted_terminal_command


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    capability: CapabilityDefinition | None
    normalized_input: dict[str, Any] | None
    impact_level: str
    decision: PolicyDecision
    reason: str


def evaluate_proposal(
    *,
    capability_id: str,
    input_payload: dict[str, Any],
    pending_approval_exists: bool,
    influenced_by_untrusted_content: bool = False,
    provenance_status: Literal["clean", "tainted", "ambiguous"] | None = None,
) -> PolicyEvaluation:
    capability = get_capability(capability_id)
    if capability is None:
        return PolicyEvaluation(
            capability=None,
            normalized_input=None,
            impact_level="read",
            decision="deny",
            reason="unknown_capability",
        )

    normalized_input, input_error = capability.validate_input(input_payload)
    if input_error is not None or normalized_input is None:
        return PolicyEvaluation(
            capability=capability,
            normalized_input=None,
            impact_level=capability.impact_level,
            decision="deny",
            reason="schema_invalid",
        )

    if capability_id in {"cap.terminal.run", "cap.terminal.run_background"}:
        cwd = normalized_input["cwd"]
        command = normalized_input["command"]
        if denylisted_terminal_command(cwd, command) is not None:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="terminal_command_denied_secret_path",
            )
        if influenced_by_untrusted_content or provenance_status in {"tainted", "ambiguous"}:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="terminal_command_denied_untrusted_input",
            )
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        lowered_words = [word.lower() for word in words]
        safe_read = False
        if lowered_words:
            if lowered_words[0] == "git" and len(lowered_words) > 1:
                safe_read = lowered_words[1] in {
                    "status",
                    "log",
                    "show",
                    "diff",
                    "branch",
                    "rev-parse",
                    "ls-files",
                    "grep",
                }
            elif lowered_words[0] in {
                "pwd",
                "ls",
                "rg",
                "head",
                "tail",
                "wc",
            }:
                safe_read = True
        lowered_command = command.lower()
        denied_tokens = {"| sh", "| bash"}
        if any(token in lowered_command for token in denied_tokens):
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="terminal_command_denied_risky",
            )
        risky_shell_tokens = {
            ">",
            ">>",
            "|",
            "&&",
            "||",
            ";",
            "<",
            "`",
            "$(",
            "$",
            "~",
            "*",
            "?",
            "[",
            "]",
            "{",
            "}",
            "://",
        }
        network_commands = {"curl", "wget", "ssh", "scp", "rsync"}
        production_commands = {"docker", "kubectl", "psql"}
        network_command_seen = any(word in network_commands for word in lowered_words) or any(
            token in lowered_command
            for token in (
                "curl ",
                "curl\t",
                "/curl",
                "wget ",
                "wget\t",
                "/wget",
                "ssh ",
                "ssh\t",
                "/ssh",
                "scp ",
                "scp\t",
                "/scp",
                "rsync ",
                "rsync\t",
                "/rsync",
            )
        )
        production_command_seen = any(word in production_commands for word in lowered_words) or any(
            token in lowered_command
            for token in (
                "docker ",
                "docker\t",
                "/docker",
                "kubectl ",
                "kubectl\t",
                "/kubectl",
                "psql ",
                "psql\t",
                "/psql",
            )
        )
        risky_commands = {
            "sudo",
            "rm",
            "mv",
            "cp",
            "chmod",
            "chown",
            "mkdir",
            "touch",
            "python",
            "python3",
            "node",
            "npm",
            "pnpm",
            "npx",
            "uv",
            "pip",
            "make",
        }
        risky_option = False
        if lowered_words:
            if lowered_words[0] == "find" and any(
                word in {"-delete", "-exec", "-execdir"} for word in lowered_words
            ):
                risky_option = True
            if lowered_words[0] == "sed" and any(
                word == "-i" or word.startswith("-i.") or word.startswith("-i/")
                for word in lowered_words
            ):
                risky_option = True
        inside_worktree = False
        try:
            inside_worktree = Path(cwd).resolve().is_relative_to(Path.cwd().resolve())
        except OSError:
            inside_worktree = False
        if safe_read and inside_worktree:
            for word in words[1:]:
                if word.startswith("-") or "://" in word:
                    continue
                if "/" not in word and not word.startswith("."):
                    continue
                try:
                    if not (Path(cwd) / word).resolve().is_relative_to(Path(cwd).resolve()):
                        safe_read = False
                        break
                except OSError:
                    safe_read = False
                    break
        if (
            safe_read
            and inside_worktree
            and not any(token in lowered_command for token in risky_shell_tokens)
            and not network_command_seen
            and not production_command_seen
            and not any(word in risky_commands for word in lowered_words)
            and not risky_option
        ):
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="allow_inline",
                reason="terminal_safe_read",
            )
        if pending_approval_exists:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="pending_approval_limit_reached",
            )
        if network_command_seen:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level="external_send",
                decision="requires_approval",
                reason="terminal_network_command_requires_approval",
            )
        if production_command_seen:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level="write_irreversible",
                decision="requires_approval",
                reason="terminal_production_command_requires_approval",
            )
        return PolicyEvaluation(
            capability=capability,
            normalized_input=normalized_input,
            impact_level="write_reversible",
            decision="requires_approval",
            reason="terminal_command_requires_approval",
        )

    if capability.policy_decision == "deny":
        return PolicyEvaluation(
            capability=capability,
            normalized_input=normalized_input,
            impact_level=capability.impact_level,
            decision="deny",
            reason="policy_denied",
        )

    is_side_effecting = capability.impact_level != "read"
    effective_taint = influenced_by_untrusted_content
    if provenance_status is not None:
        effective_taint = provenance_status in {"tainted", "ambiguous"}
    if is_side_effecting and effective_taint:
        if capability.impact_level in {"write_irreversible", "external_send"}:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="taint_denied_untrusted_side_effect",
            )
        if pending_approval_exists:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="pending_approval_limit_reached",
            )
        return PolicyEvaluation(
            capability=capability,
            normalized_input=normalized_input,
            impact_level=capability.impact_level,
            decision="requires_approval",
            reason="taint_escalated_requires_approval",
        )

    if capability.policy_decision == "requires_approval":
        if pending_approval_exists:
            return PolicyEvaluation(
                capability=capability,
                normalized_input=normalized_input,
                impact_level=capability.impact_level,
                decision="deny",
                reason="pending_approval_limit_reached",
            )
        return PolicyEvaluation(
            capability=capability,
            normalized_input=normalized_input,
            impact_level=capability.impact_level,
            decision="requires_approval",
            reason="approval_required",
        )

    return PolicyEvaluation(
        capability=capability,
        normalized_input=normalized_input,
        impact_level=capability.impact_level,
        decision="allow_inline",
        reason="allowlisted_read",
    )
