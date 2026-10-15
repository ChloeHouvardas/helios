"""The deployment contract: helios.yaml loading and the secret linter.

Offline by construction — `engine.contracts` is pure parsing and regex, and it
never touches Docker, the network or a daemon.

Why this suite exists: the manifest contract is the deployment boundary every
app adopts, and the linter is the last static gate before a bundle is built
into an image. A linter that flags a documented placeholder, or a loader that
accepts an out-of-range port, both fail at the worst possible moment — after a
build, in front of a judge.

Ported to the p2 engine's contract. Dropped from the original p0 suite:
- the `("", "app")` case of test_name_is_sanitised_to_a_dns_safe_slug: p2
  rejects an empty name outright instead of falling back to "app" (now
  pinned by test_an_empty_name_is_rejected).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from engine.contracts import (
    ALLOWED_TOOLS,
    MANIFEST_NAME,
    SECRET_PATTERNS,
    ContractError,
    Manifest,
    lint,
    load_manifest,
    sanitize,
)


# --- helpers ---------------------------------------------------------------

def write_manifest(app_dir: Path, body: str) -> Path:
    app_dir.mkdir(parents=True, exist_ok=True)
    path = app_dir / MANIFEST_NAME
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def write_file(app_dir: Path, name: str, body: str) -> Path:
    path = app_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --- load_manifest: defaults ----------------------------------------------

def test_defaults_fill_in_every_omitted_field(tmp_path: Path) -> None:
    """Only `name` is required; everything else has a documented default."""
    write_manifest(tmp_path, """
        name: collect
    """)
    manifest = load_manifest(tmp_path)
    assert manifest.name == "collect"
    assert manifest.tenant == ""
    assert manifest.entry == "main:app"
    assert manifest.port == 3000
    assert manifest.permissions == []


def test_the_declared_defaults_match_the_model(tmp_path: Path) -> None:
    """The model's own defaults, so a silent change shows up as a diff."""
    write_manifest(tmp_path, "name: x\n")
    manifest = load_manifest(tmp_path)
    fresh = Manifest(name="x")
    assert manifest.entry == fresh.entry
    assert manifest.port == fresh.port
    assert manifest.permissions == fresh.permissions


def test_loads_the_sample_app_manifest() -> None:
    """The shipped sample bundle is itself a valid manifest."""
    sample = Path(__file__).resolve().parents[1] / "examples" / "sample-app"
    manifest = load_manifest(sample)
    assert manifest.name == "sample-collect"
    assert manifest.tenant == "demo"
    assert manifest.entry == "main:app"
    assert manifest.port == 3000
    assert manifest.permissions == ["payments", "email", "knowledge-graph", "events"]
    assert set(manifest.permissions) <= ALLOWED_TOOLS


# --- load_manifest: name sanitisation --------------------------------------

def test_a_missing_manifest_is_a_contract_error(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match=MANIFEST_NAME):
        load_manifest(tmp_path)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("My_App", "my-app"),
        ("Hello World!", "hello-world"),
        ("UPPER", "upper"),
        ("  padded  ", "padded"),
        ("---dashes---", "dashes"),
        ("!!!", "app"),           # nothing survives -> the fallback
        ("__init__", "init"),     # leading non-alphanumerics stripped
    ],
)
def test_name_is_sanitised_to_a_dns_safe_slug(
    tmp_path: Path, raw: str, expected: str
) -> None:
    """The name becomes a hostname and a container name, so it must be safe.

    `sanitize` lowercases, maps every disallowed character to a hyphen, strips
    leading/trailing hyphens, then strips any remaining leading
    non-alphanumeric run, and falls back to "app" when nothing survives.
    """
    write_manifest(tmp_path, f'name: "{raw}"\n')
    assert load_manifest(tmp_path).name == expected


def test_an_empty_name_is_rejected(tmp_path: Path) -> None:
    """An empty name is a missing name, not a request for the "app" fallback."""
    write_manifest(tmp_path, 'name: ""\n')
    with pytest.raises(ContractError, match="'name' is required"):
        load_manifest(tmp_path)


def test_tenant_is_sanitised_the_same_way(tmp_path: Path) -> None:
    write_manifest(tmp_path, 'name: a\ntenant: "Demo Tenant"\n')
    assert load_manifest(tmp_path).tenant == "demo-tenant"


def test_an_empty_tenant_stays_empty(tmp_path: Path) -> None:
    """Empty means "no tenant", not "sanitize the empty string into 'app'"."""
    write_manifest(tmp_path, "name: a\ntenant: ''\n")
    assert load_manifest(tmp_path).tenant == ""


@pytest.mark.parametrize("value", ["a-b", "abc123", "A-B", "x_y", "plain"])
def test_sanitize_is_idempotent_on_safe_input(value: str) -> None:
    once = sanitize(value)
    assert sanitize(once) == once


def test_sanitize_never_returns_empty() -> None:
    """A blank slug would produce an unaddressable container name."""
    for hostile in ("", "-", "---", "_", "...", "!!!", "\t\n"):
        assert sanitize(hostile) == "app"


# --- load_manifest: bad port -----------------------------------------------

@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_an_out_of_range_port_is_rejected(tmp_path: Path, port: int) -> None:
    write_manifest(tmp_path, f"name: a\nport: {port}\n")
    with pytest.raises(ContractError, match=rf"'port' {port} is outside 1-65535"):
        load_manifest(tmp_path)


@pytest.mark.parametrize("port", [1, 80, 3000, 8080, 65535])
def test_the_port_range_boundaries_are_accepted(tmp_path: Path, port: int) -> None:
    write_manifest(tmp_path, f"name: a\nport: {port}\n")
    assert load_manifest(tmp_path).port == port


def test_a_non_numeric_port_is_rejected(tmp_path: Path) -> None:
    write_manifest(tmp_path, "name: a\nport: not-a-port\n")
    with pytest.raises(ContractError, match="'port' must be an integer"):
        load_manifest(tmp_path)


# --- load_manifest: bad permissions ----------------------------------------

def test_an_unknown_permission_is_rejected_and_names_the_alternatives(
    tmp_path: Path,
) -> None:
    """The error must say what to write instead, not just that it is wrong."""
    write_manifest(tmp_path, "name: a\npermissions:\n  - payments\n  - shell\n")
    with pytest.raises(ContractError) as excinfo:
        load_manifest(tmp_path)
    message = str(excinfo.value)
    assert "unknown permission 'shell'" in message
    for allowed in sorted(ALLOWED_TOOLS):
        assert allowed in message
    # The valid entry next to it is not reported as a problem.
    assert "unknown permission 'payments'" not in message


def test_every_allowed_permission_is_accepted(tmp_path: Path) -> None:
    body = "name: a\npermissions:\n" + "".join(f"  - {p}\n" for p in sorted(ALLOWED_TOOLS))
    write_manifest(tmp_path, body)
    assert set(load_manifest(tmp_path).permissions) == ALLOWED_TOOLS


def test_an_empty_permission_list_is_allowed(tmp_path: Path) -> None:
    """A read-only app is a legitimate app; absence of permission is not an error."""
    write_manifest(tmp_path, "name: a\npermissions: []\n")
    assert load_manifest(tmp_path).permissions == []


# --- load_manifest: malformed documents ------------------------------------

def test_a_non_mapping_document_is_rejected(tmp_path: Path) -> None:
    write_manifest(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ContractError, match="must be a mapping"):
        load_manifest(tmp_path)


def test_invalid_yaml_is_reported_as_a_contract_error(tmp_path: Path) -> None:
    write_manifest(tmp_path, "name: a\n  bad: indentation: here\n")
    with pytest.raises(ContractError, match="not valid YAML"):
        load_manifest(tmp_path)


def test_a_missing_name_is_rejected(tmp_path: Path) -> None:
    write_manifest(tmp_path, "port: 3000\n")
    with pytest.raises(ContractError, match="'name' is required"):
        load_manifest(tmp_path)


def test_an_empty_file_is_rejected(tmp_path: Path) -> None:
    """`yaml.safe_load` returns None for an empty file; that is not a manifest."""
    write_manifest(tmp_path, "")
    with pytest.raises(ContractError, match="'name' is required"):
        load_manifest(tmp_path)


# --- the secret linter -----------------------------------------------------

#: One triggering sample per declared pattern, so adding a pattern without a
#: test for it leaves an obvious gap.
SECRET_SAMPLES = {
    "OpenAI-style API key": "sk-abcdefghij0123456789ABCDEF",
    "AWS access key id": "AKIAIOSFODNN7EXAMPLE",
    "GitHub personal access token": "ghp_" + "A" * 36,
    "Slack token": "xoxb-1234567890-abcdefghij",
    "private key": "-----BEGIN OPENSSH PRIVATE KEY-----",
}


def test_every_declared_pattern_has_a_sample_here() -> None:
    """Guards the table above against drifting from SECRET_PATTERNS."""
    declared = {label for _, label in SECRET_PATTERNS}
    assert declared == set(SECRET_SAMPLES)


@pytest.mark.parametrize("label", sorted(SECRET_SAMPLES))
def test_the_linter_flags_each_secret_shape(tmp_path: Path, label: str) -> None:
    write_file(tmp_path, "main.py", f'API = "{SECRET_SAMPLES[label]}"\n')
    problems = lint(tmp_path)
    assert len(problems) == 1
    assert label in problems[0]
    assert "main.py" in problems[0]


def test_a_clean_bundle_reports_no_problems(tmp_path: Path) -> None:
    write_manifest(tmp_path, "name: clean\nport: 3000\npermissions:\n  - payments\n")
    write_file(
        tmp_path,
        "main.py",
        """
        import os
        from fastapi import FastAPI

        app = FastAPI()
        # Credentials come from the environment, never from source.
        TOKEN = os.environ["HELIOS_ACTION_TOKEN"]


        @app.get("/health")
        def health():
            return {"ok": True}
        """,
    )
    write_file(tmp_path, "notes.txt", "sk- is a prefix we mention in prose only\n")
    assert lint(tmp_path) == []


def test_the_zero_placeholder_passes_the_linter(tmp_path: Path) -> None:
    """A 64-character all-zero digest is a *placeholder*, not a credential.

    Docs, fixtures and smoke-test env files stand in a fake execution secret
    with 64 zeros. Flagging it would make every documented example fail the
    linter, which trains people to ignore the linter — worse than not having
    one. Real secrets are high-entropy, so a genuine key still trips its
    pattern; this asserts only that the placeholder shape is clean.

    The filenames matter: `Path(".env").suffix` is `""`, not `".env"`, so a
    bare dotfile is not scanned at all. These use scanned extensions so the
    assertion really exercises the patterns rather than passing vacuously.
    """
    placeholder = "0" * 64
    assert len(placeholder) == 64
    write_file(
        tmp_path,
        "secrets.env",
        f"HELIOS_EXECUTION_SECRET={placeholder}\n",
    )
    write_file(tmp_path, "main.py", f'FILLER = "{placeholder}"\n')
    problems = lint(tmp_path)
    assert problems == []


def test_a_real_secret_next_to_a_placeholder_is_still_flagged(tmp_path: Path) -> None:
    """The placeholder exemption must not become a blanket one."""
    write_file(
        tmp_path,
        "secrets.env",
        f"FAKE={'0' * 64}\n"
        f'REAL="{SECRET_SAMPLES["OpenAI-style API key"]}"\n',
    )
    problems = lint(tmp_path)
    assert len(problems) == 1
    assert "OpenAI-style API key" in problems[0]


def test_a_bare_dotfile_env_is_not_scanned(tmp_path: Path) -> None:
    """Records real behaviour, so a future change is a deliberate one.

    `TEXT_EXT` is matched against `Path.suffix`, and `Path(".env").suffix` is
    `""`. So a file named exactly `.env` — the conventional place a secret
    actually lives — is invisible to this linter, while `secrets.env` is
    caught. That is a genuine gap in the imported engine's coverage. It is
    left as-is here because P0 must not change teammate behaviour, but it is
    pinned by a test so nobody assumes `.env` is covered. Real protection
    comes from `.env` being gitignored, which it is.
    """
    secret = SECRET_SAMPLES["OpenAI-style API key"]
    write_file(tmp_path, ".env", f'API_KEY="{secret}"\n')
    assert lint(tmp_path) == []

    write_file(tmp_path, "secrets.env", f'API_KEY="{secret}"\n')
    assert len(lint(tmp_path)) == 1


def test_the_linter_reports_every_offending_file(tmp_path: Path) -> None:
    """It is a report, not a first-failure abort: all problems at once."""
    write_file(tmp_path, "a.py", f'K = "{SECRET_SAMPLES["AWS access key id"]}"\n')
    write_file(tmp_path, "sub/b.json", f'{{"k": "{SECRET_SAMPLES["Slack token"]}"}}\n')
    problems = lint(tmp_path)
    assert len(problems) == 2
    reported = {str(Path(p.split(":")[0])) for p in problems}
    assert reported == {"a.py", str(Path("sub") / "b.json")}


def test_the_linter_skips_vendored_and_cache_directories(tmp_path: Path) -> None:
    """`.venv` and friends are not the app's code and are huge."""
    secret = SECRET_SAMPLES["OpenAI-style API key"]
    write_file(tmp_path, ".venv/lib/site-packages/x.py", f'K = "{secret}"\n')
    write_file(tmp_path, "__pycache__/x.pyc.py", f'K = "{secret}"\n')
    write_file(tmp_path, "node_modules/x/y.js", f'K = "{secret}"\n')
    write_file(tmp_path, ".git/config.py", f'K = "{secret}"\n')
    assert lint(tmp_path) == []


def test_the_linter_ignores_binary_extensions(tmp_path: Path) -> None:
    secret = SECRET_SAMPLES["OpenAI-style API key"]
    write_file(tmp_path, "logo.png", f'K = "{secret}"\n')
    write_file(tmp_path, "bundle.zip", f'K = "{secret}"\n')
    assert lint(tmp_path) == []


def test_the_linter_covers_every_text_extension(tmp_path: Path) -> None:
    """Each declared text extension really is scanned."""
    secret = SECRET_SAMPLES["OpenAI-style API key"]
    for ext in (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".env", ".txt",
                ".sh", ".cfg", ".ini", ".toml"):
        write_file(tmp_path, f"probe{ext}", f'K = "{secret}"\n')
    problems = lint(tmp_path)
    assert len(problems) == 12, problems
    assert {p.split(":")[0] for p in problems} == {
        f"probe{ext}" for ext in
        (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".env", ".txt",
         ".sh", ".cfg", ".ini", ".toml")
    }


def test_the_linter_returns_a_list_even_for_an_empty_bundle(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    assert lint(tmp_path) == []


def test_an_empty_permission_list_grants_no_family() -> None:
    """The imported build handed a read-only app payments + email it never asked for."""
    from engine.contracts import DEFAULT_ALLOWED_TOOLS, Manifest
    assert DEFAULT_ALLOWED_TOOLS == ()
    manifest = Manifest(name="quiet", entry="server:app", port=3000, permissions=[])
    assert manifest.allowed_tools == ""
