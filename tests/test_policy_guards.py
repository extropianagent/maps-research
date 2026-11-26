"""Source-level guards: the toolkit stays inside the account-protection rules."""

import ast
import re
from pathlib import Path

from maps import cli, policy

MAPS = Path(cli.__file__).parent
FILES = sorted(MAPS.rglob("*.py"))
FORBIDDEN_IMPORTS = ("anthropic", "openai", "xai", "google.generativeai", "langchain")
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


def test_no_ai_vendor_sdk_is_imported():
    for path in FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "") + "." + a.name for a in node.names] + [node.module or ""]
            for n in names:
                for bad in FORBIDDEN_IMPORTS:
                    assert n != bad and not n.startswith(bad + "."), f"{path.name} imports {n}"


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
