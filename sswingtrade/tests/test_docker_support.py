"""Guards for the Docker deployment: LLM client against reasoning models, .env with compose-only keys,
requirements.txt vs what app/ imports, and the safety properties of docker-compose.yml."""

import ast
import json
import re
import sys
from importlib import metadata
from pathlib import Path

import httpx
import pytest
import yaml

import app.llm.client as llm_client
from app.config import Settings
from app.llm.client import OllamaClient

ROOT = Path(__file__).resolve().parents[1]


# ============================================================================= LLM client vs reasoning models
def _mock_ollama(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(llm_client.httpx, "AsyncClient",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler), timeout=k.get("timeout")))


async def test_client_switches_reasoning_off_and_asks_for_json(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"ok": true}'}})
    _mock_ollama(monkeypatch, handler)
    out = await OllamaClient(base_url="http://ollama:11434", model="qwen3:8b")._chat("system", "prompt")
    assert out == '{"ok": true}'
    # qwen3 "thinks" for minutes on CPU by default and blew the 120 s timeout; the agents only need the JSON
    assert seen[0]["think"] is False and seen[0]["format"] == "json" and seen[0]["model"] == "qwen3:8b"


async def test_client_retries_without_think_when_the_server_rejects_it(monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append("think" in body)
        if "think" in body:
            return httpx.Response(400, json={"error": "\"llama3.2:3b\" does not support think"})
        return httpx.Response(200, json={"message": {"content": "{}"}})
    _mock_ollama(monkeypatch, handler)
    assert await OllamaClient(model="llama3.2:3b")._chat("s", "p") == "{}"
    assert seen == [True, False]


async def test_other_http_errors_are_not_swallowed(monkeypatch):
    _mock_ollama(monkeypatch, lambda request: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(httpx.HTTPStatusError):
        await OllamaClient(model="m")._chat("s", "p")


# ============================================================================= .env shared with docker compose
def test_env_file_with_compose_only_keys_does_not_crash_the_app(tmp_path):
    env = tmp_path / ".env"
    env.write_text("POSTGRES_PASSWORD=x\nREDIS_PASSWORD=y\nGRAFANA_ADMIN_PASSWORD=z\nAPP_ENV=production\n", encoding="utf-8")
    s = Settings(_env_file=str(env))          # used to raise "Extra inputs are not permitted"
    assert s.APP_ENV == "production"


# ============================================================================= requirements.txt covers what app/ imports
def _requirement_names(path: Path):
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith(("-", "git+")):
            names.add(re.split(r"[\[<>=!~; ]", line)[0].lower().replace("_", "-"))
    return names


def _imported_third_party():
    mods = set()
    for py in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in sys.stdlib_module_names and m != "app"}


def test_runtime_requirements_cover_every_import_of_the_app():
    runtime = _requirement_names(ROOT / "requirements.txt")
    dists = metadata.packages_distributions()
    missing = []
    for mod in sorted(_imported_third_party()):
        owners = {d.lower().replace("_", "-") for d in dists.get(mod, [])}
        if not owners & runtime:
            missing.append(f"{mod} (provided by {sorted(owners) or 'not installed'})")
    assert not missing, f"imported by app/ but absent from requirements.txt: {missing}"
    # database drivers are loaded by SQLAlchemy from the URL, so no import shows them
    assert {"psycopg2-binary", "asyncpg", "greenlet"} <= runtime


def test_dev_tools_stay_out_of_the_runtime_image():
    runtime = _requirement_names(ROOT / "requirements.txt")
    assert not runtime & {"pytest", "pytest-asyncio", "pytest-cov", "aiosqlite", "black", "flake8", "isort", "mypy",
                          "pylint", "pre-commit", "langchain", "polars", "ta"}
    assert {"pytest", "aiosqlite"} <= _requirement_names(ROOT / "requirements-dev.txt")


# ============================================================================= docker-compose.yml safety
@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_every_published_port_is_bound_to_localhost(compose):
    for name, svc in compose["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} exposes {port} to the whole network"
    assert "ports" not in compose["services"]["redis"]            # only the API talks to Redis


def test_real_trading_is_pinned_off_and_secrets_are_required(compose):
    api_env = compose["services"]["api"]["environment"]
    assert api_env["ENABLE_REAL_TRADING"] == "false" and api_env["ENABLE_PAPER_TRADING"] == "true"
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for var in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "GRAFANA_ADMIN_PASSWORD", "SECRET_KEY"):
        assert "${%s:?" % var in text, f"{var} must be mandatory (no silent default)"
    for weak in ("trading_dev_pwd", "dev-secret-key", "admin:admin"):
        assert weak not in text and weak not in (ROOT / "docker/grafana/provisioning/datasources/postgres.yml").read_text(encoding="utf-8")


def test_stack_uses_the_host_ollama_and_drops_what_is_useless(compose):
    assert set(compose["services"]) == {"postgres", "redis", "api", "grafana"}      # no ollama container, no prometheus
    assert "host.docker.internal" in compose["services"]["api"]["environment"]["OLLAMA_BASE_URL"]
    assert "host.docker.internal:host-gateway" in compose["services"]["api"]["extra_hosts"]
    assert "--reload" not in json.dumps(compose["services"]["api"])              # reload would kill the scheduled jobs


def test_dockerfile_installs_the_openmp_runtime_and_runs_one_worker():
    text = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    assert "libgomp1" in text                  # LightGBM / XGBoost cannot be imported without it on python-slim
    assert '"--workers", "1"' in text          # the scheduler lives inside the process
    assert (ROOT / ".dockerignore").read_text(encoding="utf-8").count(".env") >= 1   # secrets never enter the image
