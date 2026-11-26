"""Account protection: the single place that decides what this toolkit may do.

The owner's rule: avoid genuinely risky methods, not powerful ones. The rules are enforced here, in
code, so they hold whichever AI is driving the CLI. Approvals and stops are JSON files in the repo's
`policy/` folder (see `config.policy_dir`), so they survive ephemeral machines.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    "Read a conditional provider's own terms before approving it; the record holds what you verified",
]
# A stop on one of these blocks the others: they share one key or one vendor account.
FAMILIES = (
    {"google", "google_aggregate", "google_agent_tools", "gemini_maps"},
    {"apify", "apify_contributor"},
)
PROVIDER_DETAILS = ("data_rights", "retrieval_method", "downstream_use")
REQUIRED_DETAILS = {
    "provider": PROVIDER_DETAILS,
    "ai_host": ("data_handling",),
    "egress_proxy": ("purpose",),
}


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
    elif kind == "egress_proxy":
        if not (subject or "").strip():
            raise PolicyViolation("an egress proxy approval needs the proxy's host name")
        required = ("purpose",)
    else:
        raise PolicyViolation(f"unknown approval kind {kind!r}; use 'provider', 'ai_host' or 'egress_proxy'")
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
    kind = r.get("kind")
    required = REQUIRED_DETAILS.get(kind) if isinstance(kind, str) else None
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


def _stop_problem(s: Any) -> str | None:
    """Why a stored stop is not usable, or None."""
    if not isinstance(s, dict):
        return "not an object"
    if not isinstance(s.get("provider"), str) or not s["provider"].strip():
        return "no provider"
    if not isinstance(s.get("open"), bool):
        return "open is not true or false"
    return None


def open_stops() -> list[dict[str, Any]]:
    return [s for s in _read("stops.json") if _stop_problem(s) is None and s["open"]]


def invalid_stops() -> list[dict[str, Any]]:
    """Stored stops that are malformed, with the reason; they stop nothing."""
    return [{"record": s, "problem": p} for s in _read("stops.json") if (p := _stop_problem(s))]


def clear_stop(provider: str, *, owner_statement: str) -> int:
    """Release a provider. The OWNER's decision: needs their own statement."""
    if not (owner_statement or "").strip():
        raise PolicyViolation("clearing a stop needs the owner's own statement (owner_statement)")
    stops = _read("stops.json")
    n = 0
    for s in stops:
        if _stop_problem(s) is None and s["open"] and s["provider"] == provider:
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
        scheme, authority = m.group(1).lower(), m.group(2)
        if "@" in authority and ":" in authority.split("@", 1)[0]:
            return _CREDENTIAL_MSG
        host = authority.rsplit("@", 1)[-1].lower()
        if scheme.startswith("socks") or "proxy" in host:
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
    stripped = (key or "").strip()
    low = stripped.casefold()
    if stripped.startswith(("1//", "eyJ")):
        raise PolicyViolation(
            "the Google key looks like a refresh token (1//) or a signed token (eyJ); this toolkit "
            "only accepts an API key from a dedicated Google Cloud project"
        )
    if "ya29." in low or low.startswith("bearer "):
        raise PolicyViolation(
            "the Google key looks like a personal OAuth token (ya29. or a Bearer value); this toolkit "
            "only accepts an API key from a dedicated Google Cloud project"
        )


_RULES = r"(?:terms|polic|acceptable use|guideline)"
_GEMINI_MODEL = re.compile(r"^gemini-[a-z0-9][a-z0-9.\-]*$")


def check_gemini_model(model: str) -> str:
    """Maps grounding runs only on Gemini models: refuse any other name (it ends up in the URL path)."""
    name = (model or "").strip().removeprefix("models/")
    if not _GEMINI_MODEL.match(name):
        raise PolicyViolation(
            f"Maps grounding runs only on Gemini models; {model!r} is not a Gemini model name "
            "(MAPS_GEMINI_MODEL must look like gemini-2.5-flash)"
        )
    return name


_ENFORCEMENT_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bsuspended\b",
        r"\bsuspension\b",
        r"\bbanned\b",
        r"\bban\b(?=[\s\S]{0,40}\b(?:account|project|key|ip|access)\b)",
        r"\b(?:account|project|key|ip|access)\b(?=[\s\S]{0,40}\bban\b)[\s\S]{0,40}\bban\b",
        r"\bhas been terminated\b",
        r"\b(?:account|project|access)\b[^.\n]{0,60}\bterminated\b",
        r"\babus(?:e|ed|ive)\b",
        r"\bmisuse\b",
        r"unusual (?:traffic|activity)",
        r"automated quer",
        r"\bcaptcha\b",
        r"terms of service",
        r"acceptable use",
        rf"\bviolat\w*[\s\S]{{0,60}}{_RULES}",
        rf"{_RULES}[\s\S]{{0,60}}\bviolat\w*",
    )
)
_ACCOUNT_BLOCKED = re.compile(r"\baccount\b[^.\n]{0,60}\b(?:blocked|disabled|closed)\b", re.IGNORECASE)
ENFORCEMENT_SCAN_CHARS = 8192


def enforcement_text(body: str | bytes | dict[str, Any] | list[Any] | None) -> str:
    """The human-readable text of a response body, for enforcement detection.

    JSON bodies yield only their string VALUES (never key names such as `fieldViolations`); a value
    that is itself JSON is unpacked. Anything else is returned as it is. At most 8,192 characters.
    """
    if body is None:
        return ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    out: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if isinstance(node, str):
            stripped = node.strip()
            if depth < 6 and stripped[:1] in ("{", "["):
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    walk(parsed, depth + 1)
                    return
            out.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v, depth)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth)

    walk(body)
    return "\n".join(out)[:ENFORCEMENT_SCAN_CHARS]


def detect_enforcement(status_code: int, text: str | None) -> str | None:
    """A short reason when a response looks like enforcement rather than an ordinary error.

    Pass the text through `enforcement_text` first.
    """
    if status_code == 451:
        return "HTTP 451 (unavailable for legal reasons)"
    text = text or ""
    for rx in _ENFORCEMENT_PATTERNS:
        m = rx.search(text)
        if m:
            return f"response mentions {m.group(0).strip()[:60]!r}"
    if "billing" not in text.lower():
        m = _ACCOUNT_BLOCKED.search(text)
        if m:
            return f"response mentions {m.group(0).strip()[:60]!r}"
    return None


# --- proxies from the environment ---------------------------------------------------------------

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_proxy_env() -> None:
    """Refuse a proxy set in the environment unless it is loopback or the owner recorded it."""
    for var in PROXY_VARS:
        value = (os.environ.get(var) or "").strip()
        if not value:
            continue
        try:
            parts = urlsplit(value if "://" in value else f"//{value}")
            host = (parts.hostname or "").lower()
            userinfo = parts.username is not None or parts.password is not None
        except ValueError:
            raise PolicyViolation(f"{var} is set but is not a valid proxy URL; unset it") from None
        if userinfo:
            raise PolicyViolation(
                f"{var} carries credentials in the proxy URL (user:pass@); this toolkit does not send "
                "requests through a proxy that needs embedded credentials. Unset it"
            )
        if _is_loopback(host):
            continue
        record = approved(host, "egress_proxy") if host else None
        if record is None:
            raise PolicyViolation(
                f"{var} sets a proxy ({host or 'unparsable host'}) in the environment, and the toolkit does "
                "not route requests through an unrecorded proxy (proxy rotation to get around blocks is "
                "excluded). If it is a legitimate network proxy, the owner records it: maps policy "
                f'approve {host} --kind egress_proxy --owner-statement "<owner\'s words>" '
                '--purpose "<why this network proxy is needed>"'
            )


def proxy_status() -> dict[str, Any]:
    """The proxy check as a status document: never raises."""
    try:
        check_proxy_env()
    except PolicyViolation as e:
        return {"ok": False, "problem": str(e)}
    return {"ok": True, "problem": None}


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
        "invalid_stops": invalid_stops(),
        "proxy": proxy_status(),
        "policy_dir": {"path": str(config.policy_dir()), "location": config.policy_location()},
        "ai_host": {"env": host, "approved": bool(host and approved(host, "ai_host"))},
        "retention_cap_days": config.MAX_RETENTION_DAYS,
        "state_dir": check,
        "owner_responsibilities": list(OWNER_RESPONSIBILITIES),
    }
