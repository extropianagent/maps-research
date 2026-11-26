"""Source-level guards: the toolkit stays inside the account-protection rules."""

import ast
import re
import sys
from pathlib import Path

from maps import cli, policy

MAPS = Path(cli.__file__).parent
FILES = sorted(MAPS.rglob("*.py"))
AI_HOSTS = (
    "openai.com",
    "chatgpt.com",
    "anthropic.com",
    "claude.ai",
    "x.ai",
    "grok.com",
    "gemini.google.com",
    "aistudio.google.com",
    "perplexity.ai",
    "mistral.ai",
    "deepseek.com",
    "cohere.com",
    "cohere.ai",
    "aiplatform.googleapis.com",
)
OFFICIAL_AI_HOST = "generativelanguage.googleapis.com"


def test_there_are_source_files_to_scan():
    assert len(FILES) > 15 and MAPS / "policy.py" in FILES


# --- allow-lists (frozen; adding to one is a reviewed decision, see docs/account-protection.md) -------

ALLOWED_THIRD_PARTY_IMPORTS = frozenset({"click", "fastembed", "httpx", "maps", "mcp", "uvicorn"})
ALLOWED_HOSTS = frozenset(
    {
        # endpoints the providers call
        "places.googleapis.com",
        "areainsights.googleapis.com",
        "mapstools.googleapis.com",
        "generativelanguage.googleapis.com",
        "api.apify.com",
        "api.dataforseo.com",
        # contributor profile URLs the toolkit builds (never fetched directly)
        "www.google.com",
        # documentation links that appear only in docstrings and comments
        "apify.com",
        "docs.apify.com",
        "docs.dataforseo.com",
        # a fake link in the simulator's synthetic places
        "maps.example",
    }
)
PROXY_KEYWORDS = frozenset({"proxy", "proxies", "proxy_url", "mounts", "trust_env"})
_URL_RE = re.compile(r"https?://([^\s/\"'`)>\]\\]+)")


def import_violations(source: str, allowed: frozenset[str] = ALLOWED_THIRD_PARTY_IMPORTS) -> list[str]:
    """Top-level modules imported by `source` that are neither standard library nor allow-listed."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return sorted(m for m in found if m not in sys.stdlib_module_names and m not in allowed)


def host_violations(source: str, allowed: frozenset[str] = ALLOWED_HOSTS) -> list[str]:
    """Hosts of `https://` URLs in string literals (docstrings included) that are not allow-listed."""
    hosts: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            hosts.update(
                m.group(1).split(":")[0].split("@")[-1].lower()
                for m in _URL_RE.finditer(node.value)
                if m.group(0).startswith("https://")
            )
    return sorted(h for h in hosts if h not in allowed)


def proxy_violations(source: str) -> list[str]:
    """Keyword arguments that configure a proxy or environment-driven proxying on a call."""
    return sorted(
        {
            kw.arg
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg in PROXY_KEYWORDS
        }
    )


def test_every_imported_module_is_standard_library_or_allow_listed():
    for path in FILES:
        bad = import_violations(path.read_text(encoding="utf-8"))
        assert not bad, f"{path.name} imports {bad}: not standard library and not in the allow-list"


def test_every_https_host_in_a_string_literal_is_allow_listed():
    for path in FILES:
        bad = host_violations(path.read_text(encoding="utf-8"))
        assert not bad, f"{path.name} mentions {bad}: not in ALLOWED_HOSTS"


def test_providers_configure_no_proxy():
    for path in sorted((MAPS / "providers").glob("*.py")):
        bad = proxy_violations(path.read_text(encoding="utf-8"))
        assert not bad, f"{path.name} passes {bad}"


def test_the_guards_can_fail_on_synthetic_sources():
    assert import_violations("import litellm\nimport json\nfrom maps import cli") == ["litellm"]
    assert import_violations("from google import genai") == ["google"]
    assert import_violations("import anthropic") == ["anthropic"]
    assert host_violations('URL = "https://openrouter.ai/api/v1"') == ["openrouter.ai"]
    assert host_violations('"""See https://docs.apify.com/x and https://api.groq.com/v1"""') == [
        "api.groq.com"
    ]
    assert host_violations('URL = f"https://api.together.xyz/{x}"') == ["api.together.xyz"]
    assert proxy_violations('c = httpx.Client(proxy="http://x")') == ["proxy"]
    assert proxy_violations("c = httpx.Client(proxies={}, trust_env=True)") == ["proxies", "trust_env"]
    # and they stay quiet on clean code
    clean = 'import json, httpx\nc = httpx.Client(timeout=3)\nU = "https://places.googleapis.com/v1"'
    assert import_violations(clean) == host_violations(clean) == proxy_violations(clean) == []


def test_the_allow_lists_are_not_padded():
    seen_imports: set[str] = set()
    seen_hosts: set[str] = set()
    for path in FILES:
        src = path.read_text(encoding="utf-8")
        seen_imports |= {
            n.split(".")[0]
            for node in ast.walk(ast.parse(src))
            for n in (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                else []
            )
        }
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                seen_hosts |= {m.group(1).split(":")[0] for m in _URL_RE.finditer(node.value)}
    assert ALLOWED_THIRD_PARTY_IMPORTS <= seen_imports, ALLOWED_THIRD_PARTY_IMPORTS - seen_imports
    assert ALLOWED_HOSTS <= seen_hosts, ALLOWED_HOSTS - seen_hosts


def test_no_consumer_or_api_ai_endpoints_are_referenced():
    for path in FILES:
        text = path.read_text(encoding="utf-8").lower()
        for host in AI_HOSTS:
            assert not re.search(rf"(?<![\w-]){re.escape(host)}", text), f"{path.name} mentions {host}"
        if path.name != "gemini_maps.py":
            assert OFFICIAL_AI_HOST not in text, f"{path.name} mentions {OFFICIAL_AI_HOST}"


def test_providers_never_mention_evasion_tooling():
    for path in sorted((MAPS / "providers").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for bad in ("proxies=", "proxyConfiguration", "captcha"):
            assert bad.lower() not in text.lower(), f"{path.name} mentions {bad}"


def test_every_provider_class_the_factories_use_is_registered():
    source = (MAPS / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    factories = {
        "_construct_providers",
        "build_providers",
        "build_agent_tools",
        "build_contributor_provider",
        "build_discoverer",
    }
    used: set[str] = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in factories:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else None
                    if name and name[0].isupper() and name not in ("CliError",):
                        used.add(name)
    classes = {
        "GooglePlaces": "google",
        "PlacesAggregate": "google_aggregate",
        "MapsAgentTools": "google_agent_tools",
        "ApifyProvider": "apify",
        "DataForSEOProvider": "dataforseo",
        "SimProvider": "sim",
        "GeminiMaps": "gemini_maps",
    }
    assert used, "no provider classes found in the factories"
    for cls in used:
        assert cls in classes, f"{cls} is not mapped to a registered provider"
        assert classes[cls] in policy.CORE | policy.CONDITIONAL
    # the contributor factory uses Apify's contributor path, registered as its own conditional provider
    assert "apify_contributor" in policy.CONDITIONAL
