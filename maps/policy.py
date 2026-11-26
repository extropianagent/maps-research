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
# Third-party extraction: usable only after a recorded approval. `outscraper` and `hasdata` are registered, not implemented.
CONDITIONAL = {"apify", "dataforseo", "apify_contributor", "outscraper", "hasdata"}
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
    {"google", "google_aggregate", "google_agent_tools", "gemini_maps"},
    {"apify", "apify_contributor"},
)
PROVIDER_DETAILS = ("data_rights", "retrieval_method", "downstream_use")
REQUIRED_DETAILS = {"provider": PROVIDER_DETAILS, "ai_host": ("data_handling",)}


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
    kept = [
        r
        for r in _read("approvals.json")
        if not (isinstance(r, dict) and (r.get("subject"), r.get("kind")) == (subject, kind))
    ]
    _write("approvals.json", [*kept, record])
    return record


def revoke(subject: str) -> int:
    """Remove every approval of `subject`; returns how many were removed."""
    records = _read("approvals.json")
    kept = [r for r in records if not (isinstance(r, dict) and r.get("subject") == subject)]
    if len(kept) != len(records):
        _write("approvals.json", kept)
    return len(records) - len(kept)


def _record_problem(r: Any) -> str | None:
    """Why a stored approval is not usable, or None when it is complete."""
    if not isinstance(r, dict):
        return "not an object"
    if not isinstance(r.get("subject"), str) or not r["subject"].strip():
        return "no subject"
    required = REQUIRED_DETAILS.get(r.get("kind"))  # type: ignore[arg-type]
    if required is None:
        return f"unknown kind {r.get('kind')!r}"
    if not isinstance(r.get("owner_statement"), str) or not r["owner_statement"].strip():
        return "no owner_statement"
    details = r.get("details")
    if not isinstance(details, dict):
        return "no details"
    missing = [k for k in required if not isinstance(details.get(k), str) or not details[k].strip()]
    return f"details missing: {', '.join(missing)}" if missing else None


def approvals() -> list[dict[str, Any]]:
    """The complete approval records."""
    return [r for r in _read("approvals.json") if _record_problem(r) is None]


def invalid_approvals() -> list[dict[str, Any]]:
    """Stored records that are malformed or incomplete, with the reason; they approve nothing."""
    return [{"record": r, "problem": p} for r in _read("approvals.json") if (p := _record_problem(r))]


def approved(subject: str, kind: str) -> dict[str, Any] | None:
    for r in _read("approvals.json"):
        if _record_problem(r) is None and r["subject"] == subject and r["kind"] == kind:
            return r
    return None


def _incomplete(subject: str, kind: str) -> bool:
    """True when a record names this subject and kind but is not usable."""
    return any(
        isinstance(r, dict) and r.get("subject") == subject and r.get("kind") == kind and _record_problem(r)
        for r in _read("approvals.json")
    )


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
        if _incomplete(name, "provider"):
            raise PolicyViolation(
                f"the approval record for {name} in policy/approvals.json is incomplete; it approves "
                f"nothing. Record it again with the owner's statement: maps policy approve {name} --kind "
                'provider --owner-statement "<owner\'s words>" --data-rights "..." --retrieval-method '
                '"..." --downstream-use "..."'
            )
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
        if _incomplete(host, "ai_host"):
            raise PolicyViolation(
                f"the approval record for AI host {host!r} in policy/approvals.json is incomplete; it "
                f"approves nothing. Record it again: maps policy approve {host} --kind ai_host "
                '--owner-statement "<owner\'s words>" --data-handling "<what the host\'s settings say>"'
            )
        raise PolicyViolation(
            f"AI host {host!r} has no recorded approval of its data settings (no training on or "
            f"retention of input): maps policy approve {host} --kind ai_host --owner-statement \"<owner's "
            'words>" --data-handling "<what the host\'s settings say>"'
        )
    return host


_BAD_KEY_TOKENS = frozenset(
    {
        "cookie", "cookies", "session", "sessionid", "password", "passwd", "pwd", "pass", "pw", "login",
        "credential", "credentials", "auth", "authorization", "token", "account", "proxy", "proxies",
        "captcha", "rotate", "residential",
    }
)  # fmt: skip
_CIRCUMVENT_TOKENS = frozenset({"proxy", "proxies", "captcha", "rotate", "residential"})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_COOKIE_VALUE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:SID|HSID|SSID|APISID|SAPISID|LSID|OSID|NID)=|(?:__Secure-|__Host-)[A-Za-z0-9_-]*="
)
_URL = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*)://([^\s/?#]*)([^\s]*)")
_CREDENTIAL_MSG = "no personal Google credentials may be sent to extraction tools"
_CIRCUMVENT_MSG = (
    "no circumvention options (proxy, CAPTCHA, session or login handling) may be sent to extraction tools"
)


def _key_tokens(key: Any) -> set[str]:
    """Word tokens of a key: split on non-alphanumerics and camelCase boundaries, lower-cased."""
    out: set[str] = set()
    for part in re.split(r"[^A-Za-z0-9]+", str(key)):
        out.update(t.lower() for t in _CAMEL.split(part) if t)
    return out


def _bad_value(text: str) -> str | None:
    """Why a string value may not be sent, or None."""
    low = text.lower()
    if _COOKIE_VALUE.search(text) or "sapisidhash" in low or "ya29." in low or text.strip().startswith("1//"):
        return _CREDENTIAL_MSG
    for m in _URL.finditer(text):
        scheme, authority, rest = m.group(1).lower(), m.group(2), m.group(3)
        if "@" in authority and ":" in authority.split("@", 1)[0]:
            return _CREDENTIAL_MSG
        if scheme.startswith("socks") or "proxy" in authority.lower() or "proxy" in rest.lower():
            return _CIRCUMVENT_MSG
    return None


def check_outbound_payload(provider: str, payload: Any) -> None:
    """Refuse a request body for a third-party extraction service that carries credentials or evasion.

    Defence in depth: payloads are built in code, so this only catches a mistake or a tampered change.
    """
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                bad = _key_tokens(k) & _BAD_KEY_TOKENS
                if bad:
                    rule = _CIRCUMVENT_MSG if bad & _CIRCUMVENT_TOKENS else _CREDENTIAL_MSG
                    raise PolicyViolation(f"{provider} request refused: key {k!r} breaks the rule: {rule}")
                stack.append(v)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        elif isinstance(node, str):
            rule = _bad_value(node)
            if rule:
                raise PolicyViolation(
                    f"{provider} request refused: a value looks like a Google credential or an evasion "
                    f"option; {rule}"
                )


def check_google_key(key: str | None) -> None:
    """Refuse anything that is not a plain API key: OAuth tokens, bearer headers, in any case."""
    low = (key or "").strip().casefold()
    if "ya29." in low or low.startswith("bearer "):
        raise PolicyViolation(
            "the Google key looks like a personal OAuth token (ya29. or a Bearer value); this toolkit "
            "only accepts an API key from a dedicated Google Cloud project"
        )


_ENFORCEMENT_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"suspen",
        r"\bban(ned|s)?\b",
        r"terminat",
        r"unusual (traffic|activity)",
        r"automated quer",
        r"captcha",
        r"abus",
        r"misuse",
        r"violat",
        r"terms of service",
        r"acceptable use",
        r"account[^.\n]{0,60}\b(blocked|disabled|closed)",
    )
)


def detect_enforcement(status_code: int, text: str | None) -> str | None:
    """A short reason when a response looks like enforcement rather than an ordinary error."""
    if status_code == 451:
        return "HTTP 451 (unavailable for legal reasons)"
    for rx in _ENFORCEMENT_PATTERNS:
        m = rx.search(text or "")
        if m:
            return f"response mentions {m.group(0).strip()!r}"
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
        "invalid_approvals": invalid_approvals(),
        "open_stops": open_stops(),
        "ai_host": {"env": host, "approved": bool(host and approved(host, "ai_host"))},
        "retention_cap_days": config.MAX_RETENTION_DAYS,
        "state_dir": check,
        "owner_responsibilities": list(OWNER_RESPONSIBILITIES),
    }
