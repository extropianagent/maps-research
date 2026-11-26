"""Account protection: the single place that decides what this toolkit may do.

The owner's rule: avoid genuinely risky methods, not powerful ones. The rules are enforced here, in
code, so they hold whichever AI is driving the CLI. Approvals and stops are JSON files in the repo's
`policy/` folder (see `config.policy_dir`), so they survive ephemeral machines.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import config
from .providers.base import PolicyStop

__all__ = ["PolicyStop", "PolicyViolation"]

CORE = {"google", "google_aggregate", "google_agent_tools", "gemini_maps", "sim"}
# Third-party extraction: usable only after a recorded approval. `outscraper` is registered, not implemented.
CONDITIONAL = {"apify", "dataforseo", "apify_contributor", "outscraper"}
EXCLUDED_METHODS = (
    "unofficial browser scraping with a Google account",
    "CAPTCHA-solving services",
    "proxy rotation to evade blocks",
    "a persistent review warehouse",
    "using consumer AI subscriptions as programmatic APIs",
)
OWNER_RESPONSIBILITIES = [
    "Use a dedicated Google Cloud project with an API key restricted to the Maps APIs",
    "Check each AI host's data settings before recording it",
]
# A stop on one of these blocks the others: they share one key or one vendor account.
FAMILIES = (
    {"google", "google_aggregate", "google_agent_tools"},
    {"apify", "apify_contributor"},
)
PROVIDER_DETAILS = ("data_rights", "retrieval_method", "downstream_use")


class PolicyViolation(RuntimeError):
    """The toolkit refuses to do this."""


# --- records ----------------------------------------------------------------------------------


def _read(name: str) -> list[dict[str, Any]]:
    path = config.policy_dir() / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise PolicyViolation(f"{path} is unreadable or not valid JSON; the owner must repair it") from None
    if not isinstance(data, list):
        raise PolicyViolation(f"{path} must hold a JSON list; the owner must repair it")
    return data


def _write(name: str, records: list[dict[str, Any]]) -> None:
    directory = config.policy_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, directory / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- approvals --------------------------------------------------------------------------------


def approve(subject: str, kind: str, *, owner_statement: str, details: dict[str, Any]) -> dict[str, Any]:
    """Record the OWNER's decision. Only call this with the owner's own words as `owner_statement`."""
    if not (owner_statement or "").strip():
        raise PolicyViolation("an approval needs the owner's own statement (owner_statement)")
    details = {k: (v.strip() if isinstance(v, str) else v) for k, v in (details or {}).items()}
    if kind == "provider":
        if subject not in CONDITIONAL:
            raise PolicyViolation(
                f"{subject!r} is not a conditional provider (conditional: {', '.join(sorted(CONDITIONAL))})"
            )
        required = PROVIDER_DETAILS
    elif kind == "ai_host":
        if not (subject or "").strip():
            raise PolicyViolation("an AI host approval needs the host's name")
        required = ("data_handling",)
    else:
        raise PolicyViolation(f"unknown approval kind {kind!r}; use 'provider' or 'ai_host'")
    missing = [k for k in required if not details.get(k)]
    if missing:
        raise PolicyViolation(f"approval for {subject} is missing: {', '.join(missing)}")
    record = {
        "subject": subject,
        "kind": kind,
        "approved_on": date.today().isoformat(),
        "owner_statement": owner_statement.strip(),
        "details": {k: details[k] for k in required},
    }
    kept = [r for r in _read("approvals.json") if (r.get("subject"), r.get("kind")) != (subject, kind)]
    _write("approvals.json", [*kept, record])
    return record


def revoke(subject: str) -> int:
    """Remove every approval of `subject`; returns how many were removed."""
    records = _read("approvals.json")
    kept = [r for r in records if r.get("subject") != subject]
    if len(kept) != len(records):
        _write("approvals.json", kept)
    return len(records) - len(kept)


def approvals() -> list[dict[str, Any]]:
    return _read("approvals.json")


def approved(subject: str, kind: str) -> dict[str, Any] | None:
    for r in _read("approvals.json"):
        if r.get("subject") == subject and r.get("kind") == kind:
            return r
    return None


# --- stops ------------------------------------------------------------------------------------


def record_stop(provider: str, detail: str) -> None:
    stops = _read("stops.json")
    stops.append(
        {
            "provider": provider,
            "detail": str(detail)[:500],
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "open": True,
        }
    )
    _write("stops.json", stops)


def open_stops() -> list[dict[str, Any]]:
    return [s for s in _read("stops.json") if s.get("open")]


def clear_stop(provider: str, *, owner_statement: str) -> int:
    """Release a provider. The OWNER's decision: needs their own statement."""
    if not (owner_statement or "").strip():
        raise PolicyViolation("clearing a stop needs the owner's own statement (owner_statement)")
    stops = _read("stops.json")
    n = 0
    for s in stops:
        if s.get("open") and s.get("provider") == provider:
            s.update(
                {
                    "open": False,
                    "cleared_on": date.today().isoformat(),
                    "clear_statement": owner_statement.strip(),
                }
            )
            n += 1
    if not n:
        raise PolicyViolation(f"{provider} has no open stop")
    _write("stops.json", stops)
    return n


def _family(name: str) -> set[str]:
    return next((f for f in FAMILIES if name in f), {name})


# --- gates ------------------------------------------------------------------------------------


def require_provider(name: str) -> None:
    if name not in CORE and name not in CONDITIONAL:
        raise PolicyViolation(
            f"{name!r} is not a registered provider; new integrations are verified and registered "
            "in maps/policy.py first"
        )
    if name in CONDITIONAL and approved(name, "provider") is None:
        raise PolicyViolation(
            f"{name} is third-party extraction and is blocked until the owner approves it. Verify its "
            "data rights (what its terms let you do with the data), its retrieval method (no logged-in "
            "Google account, no proxy or CAPTCHA evasion) and the downstream use; then record the "
            f"owner's decision: maps policy approve {name} --kind provider --owner-statement \"<owner's "
            'words>" --data-rights "..." --retrieval-method "..." --downstream-use "..."'
        )
    family = _family(name)
    for s in open_stops():
        if s.get("provider") in family:
            raise PolicyViolation(
                f"{name} is stopped after an enforcement signal from {s['provider']}: {s.get('detail')}. "
                "Tell the owner; only the owner may release it with: maps policy clear-stop "
                f'{s["provider"]} --owner-statement "<owner\'s words>"'
            )


def require_ai_host() -> str:
    host = (os.environ.get("MAPS_AI_HOST") or "").strip()
    if not host:
        raise PolicyViolation(
            "Google's grounded Maps output may only go to an AI whose data settings exclude training on "
            "and retention of its input. Name the AI host with MAPS_AI_HOST=<name> and record the owner's "
            'check: maps policy approve <name> --kind ai_host --owner-statement "<owner\'s words>" '
            '--data-handling "<what the host\'s settings say>"'
        )
    if approved(host, "ai_host") is None:
        raise PolicyViolation(
            f"AI host {host!r} has no recorded approval of its data settings (no training on or "
            f"retention of input): maps policy approve {host} --kind ai_host --owner-statement \"<owner's "
            'words>" --data-handling "<what the host\'s settings say>"'
        )
    return host


_BAD_KEYS = ("cookie", "session", "password", "passwd", "login", "credential", "proxy", "captcha")
_COOKIE_VALUE = re.compile(r"(?<![A-Za-z0-9_])(SID|HSID|SSID|SAPISID)=|__Secure-[13]PSID")
_CREDENTIAL_MSG = "no personal Google credentials may be sent to extraction tools"
_CIRCUMVENT_MSG = (
    "no circumvention options (proxy, CAPTCHA, session or login handling) may be sent to extraction tools"
)


def check_outbound_payload(provider: str, payload: Any) -> None:
    """Refuse a request body for a third-party extraction service that carries credentials or evasion."""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                low = str(k).lower()
                for bad in _BAD_KEYS:
                    if bad in low:
                        rule = _CIRCUMVENT_MSG if bad in ("proxy", "captcha") else _CREDENTIAL_MSG
                        raise PolicyViolation(
                            f"{provider} request refused: key {k!r} breaks the rule: {rule}"
                        )
                stack.append(v)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        elif isinstance(node, str):
            if _COOKIE_VALUE.search(node) or "ya29." in node:
                raise PolicyViolation(
                    f"{provider} request refused: a value looks like a Google session cookie or OAuth "
                    f"token; {_CREDENTIAL_MSG}"
                )


def check_google_key(key: str | None) -> None:
    if key and key.startswith("ya29."):
        raise PolicyViolation(
            "GOOGLE_MAPS_API_KEY looks like a personal OAuth token (ya29.); this toolkit only accepts "
            "an API key from a dedicated Google Cloud project"
        )


_ENFORCEMENT_WORDS = (
    "suspended",
    "banned",
    "abuse",
    "unusual traffic",
    "captcha",
    "violat",
    "terms of service",
    "acceptable use",
)


def detect_enforcement(status_code: int, text: str | None) -> str | None:
    """A short reason when a response looks like enforcement rather than an ordinary error."""
    if status_code == 451:
        return "HTTP 451 (unavailable for legal reasons)"
    low = (text or "").casefold()
    for word in _ENFORCEMENT_WORDS:
        if word in low:
            return f"response mentions {word.strip()!r}"
    return None


# --- state directory --------------------------------------------------------------------------


def state_dir_check(state_dir: Path | str) -> None:
    root = config.REPO_ROOT.resolve()
    path = Path(state_dir).expanduser().resolve()
    inside = path == root or root in path.parents
    allowed = (root / ".state").resolve()
    if inside and not (path == allowed or allowed in path.parents):
        raise PolicyViolation(
            f"the state directory {path} is inside the repository, where Maps content could be committed; "
            f"use a directory outside the repo or under {allowed}"
        )


def status() -> dict[str, Any]:
    state = config.state_dir()
    try:
        state_dir_check(state)
        check: dict[str, Any] = {"path": str(state), "ok": True, "problem": None}
    except PolicyViolation as e:
        check = {"path": str(state), "ok": False, "problem": str(e)}
    host = (os.environ.get("MAPS_AI_HOST") or "").strip() or None
    return {
        "registry": {"core": sorted(CORE), "conditional": sorted(CONDITIONAL)},
        "excluded_methods": list(EXCLUDED_METHODS),
        "approvals": approvals(),
        "open_stops": open_stops(),
        "ai_host": {"env": host, "approved": bool(host and approved(host, "ai_host"))},
        "retention_cap_days": config.MAX_RETENTION_DAYS,
        "state_dir": check,
        "owner_responsibilities": list(OWNER_RESPONSIBILITIES),
    }
