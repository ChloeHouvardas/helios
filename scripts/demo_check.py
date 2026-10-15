"""The DEMOFLOW.md test ladder, scripted. One line per check: id, PASS/FAIL, observed.

Rungs: T0 offline suites · T1 each app alone · T2 the builder path with a stub
agent · T3 Docker on this machine · T4 tools and logs central · T5 cross-app ·
T6 surprise task. Later rungs assume earlier ones; `all` runs them in order.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "bin" / "python"
HELIOS = REPO / "platform" / "deployment" / "cli" / "helios.py"
APPS = {
    "helios-collect": REPO / "apps" / "helios-collect",
    "helios-payables": REPO / "apps" / "helios-payables",
    "helios-cash-planner": REPO / "apps" / "helios-cash-planner",
}
RESULTS: list[tuple[str, bool, str]] = []
ENV = dict(os.environ, HELIOS_MARKET_MODE=os.environ.get("HELIOS_MARKET_MODE", "fixture"),
           PYTHONDONTWRITEBYTECODE="1")


def check(check_id: str, ok: bool, observed: str) -> bool:
    observed = " ".join(str(observed).split())[:160]
    RESULTS.append((check_id, bool(ok), observed))
    print(f"{check_id:<7} {'PASS' if ok else 'FAIL'}  {observed}", flush=True)
    return bool(ok)


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900, env=None) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd or REPO), capture_output=True,
                          text=True, timeout=timeout, env=env or ENV)


def http(method: str, url: str, body=None, headers=None, timeout: int = 60, form=None):
    data = None
    hdrs = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, raw.decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
    except urllib.error.URLError as exc:
        return 0, str(exc), {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401
        return None


_opener = urllib.request.build_opener(NoRedirect)


def http_noredirect(method: str, url: str, form=None, timeout: int = 60):
    data = urllib.parse.urlencode(form or {}).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"} if data else {})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
    except urllib.error.URLError as exc:
        return 0, str(exc), {}


import urllib.parse  # noqa: E402


# ---------------------------------------------------------------- T0
def t0(args) -> None:
    r = run([REPO / "scripts" / "test-all.sh"], timeout=1800)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    check("T0.2", r.returncode == 0 and "green" in last, last)
    r = run([PY, "-m", "agent_integration.cli", "selfcheck"])
    check("T0.3", r.returncode == 0 and "seam check: OK" in r.stdout + r.stderr, "seam check: OK" if r.returncode == 0 else r.stderr[-200:])
    r = run([PY, "-c", "from fin_tools import build_world; print(len(build_world().registry.list()))"])
    check("T0.4", r.stdout.strip() == "40", f"tools={r.stdout.strip()}")
    r = run([PY, "-m", "agent_integration.cli", "demo"])
    check("T0.5", r.returncode == 0 and "chain verification: OK" in r.stdout, "chain verification: OK" if r.returncode == 0 else r.stderr[-200:])
    r = run(["grep", "-rniE", "glm-5|zhipu|bigmodel", "--include=*.py", "--include=*.md", "--include=*.yaml",
             "--include=*.json", "--include=*.sh", "--include=*.toml", "--exclude-dir=.venv", "--exclude-dir=.qwen",
             "--exclude-dir=OTHER-TEAMMATE-STUFF", "--exclude-dir=.git", "--exclude-dir=docs", "."])
    # DEMOFLOW.md states this check's pattern and is not a reference.
    hits = [ln for ln in r.stdout.splitlines() if "demo_check.py" not in ln and "DEMOFLOW.md" not in ln]
    check("T0.6", not hits, f"{len(hits)} legacy vendor reference(s)" + (": " + hits[0] if hits else ""))


# ---------------------------------------------------------------- T1
EXPECTED = {
    "helios-collect": {"done": 5, "refused": 3, "declined_by_owner": 2},
    "helios-payables": {"done": 9, "refused": 4, "declined_by_owner": 2, "rejected_by_ledger": 1},
    "helios-cash-planner": {"done": 4, "refused": 5, "declined_by_owner": 2},
}


def _counts(stdout: str) -> dict:
    """The outcome-count dict each app prints, wherever on the line it sits."""
    for line in reversed(stdout.strip().splitlines()):
        m = re.search(r"\{'(?:done|paid)'[^}]*\}", line)
        if m:
            try:
                return json.loads(m.group(0).replace("'", '"'))
            except json.JSONDecodeError:
                continue
    return {}


def t1(args) -> None:
    for n, (name, path) in enumerate(APPS.items(), start=1):
        r = run([PY, "app.py"], cwd=path, timeout=600)
        counts = _counts(r.stdout)
        check(f"T1.{n}", r.returncode == 0 and counts == EXPECTED[name], f"{name} {counts or r.stderr[-120:]}")
    for n, path in enumerate(list(APPS.values()) + [REPO / "entry-point" / "examples" / "invoice-payer"], start=1):
        r = run([REPO / ".venv" / "bin" / "helios-app", "validate", path])
        last = (r.stdout.strip().splitlines() or [""])[-1]
        check(f"T1.6.{n}", r.returncode == 0 and "0 error(s)" in last, f"{path.name}: {last}")
    for n, path in enumerate(APPS.values(), start=1):
        r = run([PY, "-m", "pytest", "-p", "no:cacheprovider", "--override-ini=addopts=", "-q", "tests"], cwd=path, timeout=600)
        last = (r.stdout.strip().splitlines() or [""])[-1]
        check(f"T1.7.{n}", r.returncode == 0, f"{path.name}: {last}")


# ---------------------------------------------------------------- T2
def _scaffold(tmp: Path, spec: Path) -> subprocess.CompletedProcess:
    return run([REPO / ".venv" / "bin" / "helios-app", "new", tmp, "--spec", spec])


def t2(args) -> None:
    work = Path(tempfile.mkdtemp(prefix="helios-t2-"))
    spec = REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml"
    r = run([REPO / ".venv" / "bin" / "helios-app", "questions"])
    check("T2.1", r.returncode == 0 and r.stdout.count("question:") == 16, f"{r.stdout.count('question:')} questions")
    app_dir = work / "stub"
    r = _scaffold(app_dir, spec)
    written = len([p for p in app_dir.rglob("*") if p.is_file()]) if app_dir.exists() else 0
    check("T2.3", r.returncode == 0 and written >= 20, f"{written} files written")
    r2 = _scaffold(app_dir, spec)
    check("T2.3b", "0 written" in r2.stdout or "0 file" in r2.stdout or r2.returncode == 0, r2.stdout.strip().splitlines()[0] if r2.stdout.strip() else r2.stderr[-120:])
    r = run([REPO / ".venv" / "bin" / "helios-app", "validate", app_dir])
    last = (r.stdout.strip().splitlines() or [""])[-1]
    check("T2.4", r.returncode == 0 and "0 error(s)" in last, f"fresh scaffold: {last}")
    spec2 = work / "claims.yaml"
    spec2.write_text('''spec_version: "1"
identity: { id: expense-approver, name: Expense Approver }
stack: { use: helios-python-web-1 }
data:
  local:
    entities:
      - name: claim
        fields:
          - { name: id, type: string, required: true }
          - { name: amount, type: decimal, required: true }
agents:
  action_manifest: agents/expense-approver-action.json
  audit_manifest: agents/expense-approver-audit.yaml
ui:
  pages:
    - { id: holds, title: Needs your approval, pattern: approval_queue }
tools:
  - claims_lookup
  - budgets_remaining
  - reimbursements_send
''', encoding="utf-8")
    claims = work / "claims"
    _scaffold(claims, spec2)
    audit = (claims / "agents" / "expense-approver-audit.yaml").read_text(encoding="utf-8") if claims.exists() else ""
    m = re.search(r"- name: reimbursements_send.*?side_effect: (\w+)", audit, re.S)
    check("T2.5", bool(m) and m.group(1) == "true", f"reimbursements_send side_effect={m.group(1) if m else 'missing'}")
    r = run([PY, "-c", "import server"], cwd=app_dir, env=dict(ENV, PYTHONPATH=str(app_dir)))
    check("T2.6", r.returncode == 0, "generated server imports" if r.returncode == 0 else r.stderr[-160:])
    tests = (app_dir / "tests" / "test_controls.py").read_text(encoding="utf-8")
    check("T2.7", "GATED_TOOLS = []" not in tests and "GATED_TOOLS = [" in tests, "GATED_TOOLS populated")
    r = run([REPO / ".venv" / "bin" / "helios-app", "validate", REPO / "platform" / "app-builder" / "tests" / "fixtures" / "broken-app"])
    last = (r.stdout.strip().splitlines() or [""])[-1]
    check("T2.8", r.returncode == 1 and "8 error(s)" in last, f"broken-app: {last}")
    shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- T3
def _engine(args) -> str:
    return args.engine.rstrip("/")


def _deploy(args, app_dir: Path, tenant: str) -> subprocess.CompletedProcess:
    return run([PY, HELIOS, "deploy", app_dir, "--tenant", tenant, "--engine", _engine(args)], timeout=1500)


def _slug(app: str, tenant: str) -> str:
    return f"{app}-{tenant}"


def _page(slug: str, path: str = "/") -> tuple[int, str]:
    status, body, _ = http("GET", f"http://localhost/apps/{slug}{path}")
    return status, body


def _wait_page(slug: str, path: str = "/health", tries: int = 30) -> int:
    for _ in range(tries):
        status, _ = _page(slug, path)
        if status == 200:
            return 200
        time.sleep(1)
    return status


def _docker(*cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return run(["docker", *cmd], timeout=timeout)


def t3(args) -> None:
    t = args.tenant
    r = _docker("info")
    check("T3.1", r.returncode == 0, "daemon up" if r.returncode == 0 else r.stderr[-120:])
    status, body, _ = http("GET", f"{_engine(args)}/health")
    check("T3.3", status == 200 and "helios-deployment-engine" in body, body.strip()[:120])
    r = run([PY, HELIOS, "doctor", "--engine", _engine(args)])
    check("T3.4", r.returncode == 0, (r.stdout.strip().splitlines() or [r.stderr[-120:]])[0])

    sample = REPO / "platform" / "deployment" / "examples" / "sample-app"
    r = _deploy(args, sample, t)
    check("T3.5", r.returncode == 0 and _wait_page(_slug("sample-collect", t)) == 200,
          "sample-collect deployed, /health 200" if r.returncode == 0 else (r.stderr or r.stdout)[-200:])

    for n, (name, path) in enumerate(APPS.items(), start=6):
        r = _deploy(args, path, t)
        slug = _slug(name, t)
        ok = r.returncode == 0 and _wait_page(slug) == 200
        pages = {p: _page(slug, p)[0] for p in ("/", "/holds", "/health")} if ok else {}
        check(f"T3.{n}", ok and all(v == 200 for v in pages.values()),
              f"{name}: {pages}" if ok else (r.stderr or r.stdout)[-240:])

    # T3.18 all three together
    status, body, _ = http("GET", f"{_engine(args)}/api/v1/apps")
    apps = json.loads(body).get("apps", []) if status == 200 else []
    mine = [a for a in apps if a.get("tenant") == t]
    roles = {a.get("app"): [b.get("role") for b in mine if b.get("app") == a.get("app")] for a in mine}
    conf = REPO / ".helios-run" / "nginx" / "default.conf"
    blocks = conf.read_text().count("# helios:begin") if conf.exists() else -1
    running = {a.get("app") for a in mine}
    check("T3.18", {"helios-collect", "helios-payables", "helios-cash-planner"} <= running and blocks >= 4,
          f"apps={sorted(running)} nginx_blocks={blocks}")

    # T3.9 redeploy keeps serving
    r = _deploy(args, APPS["helios-collect"], t)
    status, _ = _page(_slug("helios-collect", t), "/")
    blocks_after = conf.read_text().count(f"# helios:begin {_slug('helios-collect', t)}") if conf.exists() else -1
    check("T3.9", r.returncode == 0 and status == 200 and blocks_after == 1, f"redeploy: / {status}, blocks for slug={blocks_after}")

    # T3.10 failing smoke test leaves the previous deployment serving
    broken = Path(tempfile.mkdtemp(prefix="helios-broken-")) / "sample-app"
    shutil.copytree(sample, broken)
    main = (broken / "main.py").read_text(encoding="utf-8")
    (broken / "main.py").write_text(main.replace('return {"status": "ok", "app": APP, "tenant": TENANT}',
                                                 'raise RuntimeError("deliberately unhealthy")'), encoding="utf-8")
    r = _deploy(args, broken, t)
    still = _page(_slug("sample-collect", t), "/health")[0]
    check("T3.10", r.returncode == 1 and "smoke_test" in (r.stdout + r.stderr) and still == 200,
          f"exit={r.returncode} stage=smoke_test={('smoke_test' in (r.stdout + r.stderr))} previous /health={still}")
    shutil.rmtree(broken.parent, ignore_errors=True)

    # T3.11 .env refused before upload
    with_env = Path(tempfile.mkdtemp(prefix="helios-env-")) / "sample-app"
    shutil.copytree(sample, with_env)
    (with_env / ".env").write_text("DASHSCOPE_API_KEY=sk-not-real\n")
    r = _deploy(args, with_env, t)
    check("T3.11", r.returncode == 2, f"exit={r.returncode}")
    shutil.rmtree(with_env.parent, ignore_errors=True)

    # T3.12 hard-coded secret caught by the static audit
    leaky = Path(tempfile.mkdtemp(prefix="helios-leak-")) / "sample-app"
    shutil.copytree(sample, leaky)
    (leaky / "keys.py").write_text('API_KEY = "sk-live-0123456789abcdef0123456789abcdef"\n')
    r = _deploy(args, leaky, t)
    check("T3.12", r.returncode == 1 and "static_audit" in (r.stdout + r.stderr), f"exit={r.returncode} static_audit={'static_audit' in (r.stdout + r.stderr)}")
    shutil.rmtree(leaky.parent, ignore_errors=True)

    # T3.13 unknown manifest field / permission
    bad = Path(tempfile.mkdtemp(prefix="helios-bad-")) / "sample-app"
    shutil.copytree(sample, bad)
    (bad / "helios.yaml").write_text("name: bad-app\nentry: main:app\nport: 3000\npermissions: [payments, shell]\nbogus_field: 1\n")
    r = _deploy(args, bad, t)
    out = r.stdout + r.stderr
    check("T3.13", r.returncode == 1 and "manifest" in out and "shell" in out and "bogus_field" in out,
          f"exit={r.returncode} names_both={'shell' in out and 'bogus_field' in out}")
    shutil.rmtree(bad.parent, ignore_errors=True)

    # T3.14 hold approve / decline on the deployed Collect
    slug = _slug("helios-collect", t)
    status, body, _ = http_noredirect("POST", f"http://localhost/apps/{slug}/runs", form={"invoice_id": "INV-1003"})
    _, holds_page = _page(slug, "/holds")
    hold_ids = re.findall(r"hold_[0-9a-f]{8,}", holds_page)
    approved = http_noredirect("POST", f"http://localhost/apps/{slug}/holds/{hold_ids[0]}/approve")[0] if hold_ids else 0
    again = http_noredirect("POST", f"http://localhost/apps/{slug}/holds/{hold_ids[0]}/approve")[0] if hold_ids else 0
    check("T3.14", bool(hold_ids) and approved in (303, 302) and again == 404,
          f"run={status} holds={len(hold_ids)} approve={approved} second approve={again}")

    # T3.15 logs survive a container restart
    r = _docker("restart", f"{t}-helios-collect-app")
    status = _wait_page(slug, "/runs")
    _, runs_page = _page(slug, "/runs")
    check("T3.15", r.returncode == 0 and status == 200 and "run-" in runs_page, f"restart={r.returncode} /runs={status} has runs={'run-' in runs_page}")

    # T3.17 secrets inside the app container
    r = _docker("exec", f"{t}-helios-collect-app", "env")
    envs = {ln.split("=", 1)[0] for ln in r.stdout.splitlines() if "=" in ln}
    check("T3.17", "HELIOS_ACTION_TOKEN" in envs and "HELIOS_EXECUTION_SECRET" not in envs and "HELIOS_INTENT_SECRET" not in envs,
          f"action_token={'HELIOS_ACTION_TOKEN' in envs} execution_secret={'HELIOS_EXECUTION_SECRET' in envs}")

    # T3.16 undeploy keeps the volume (sample app)
    r = run([PY, HELIOS, "undeploy", "sample-collect", "--tenant", t, "--engine", _engine(args)])
    vols = _docker("volume", "ls", "--format", "{{.Name}}").stdout.split()
    gone = _page(_slug("sample-collect", t), "/health")[0]
    check("T3.16", r.returncode == 0 and f"helios-data-{t}-sample-collect" in vols and gone != 200,
          f"undeploy={r.returncode} volume_kept={f'helios-data-{t}-sample-collect' in vols} /health now {gone}")


# ---------------------------------------------------------------- T4
def _secret(tenant: str) -> str:
    path = REPO / ".helios-run" / "tenants" / f"{tenant}.json"
    return json.loads(path.read_text())["execution_secret"] if path.exists() else ""


def _from_container(container: str, method: str, url: str, bearer: str = "", body=None) -> tuple[int, str]:
    code = (
        "import json,sys,urllib.request,urllib.error\n"
        f"req=urllib.request.Request({url!r}, method={method!r}, data={json.dumps(body).encode()!r} if {body is not None!r} else None)\n"
        f"req.add_header('Content-Type','application/json')\n"
        + (f"req.add_header('Authorization','Bearer {bearer}')\n" if bearer else "")
        + "try:\n    r=urllib.request.urlopen(req, timeout=20); print(r.status); print(r.read().decode())\n"
        "except urllib.error.HTTPError as e:\n    print(e.code); print(e.read().decode())\n"
    )
    r = _docker("exec", container, "python", "-c", code)
    lines = r.stdout.strip().splitlines()
    if not lines:
        return 0, r.stderr[-200:]
    return int(lines[0]) if lines[0].isdigit() else 0, "\n".join(lines[1:])


def t4(args) -> None:
    t = args.tenant
    audit = f"{t}-helios-collect-audit"
    appc = f"{t}-helios-collect-app"
    secret = _secret(t)
    status, body = _from_container(audit, "GET", "http://helios-core:9000/health")
    check("T4.1", status == 200 and '"tool_count"' in body, body.strip()[:120])
    status, _ = _from_container(audit, "GET", "http://helios-core:9000/tools")
    status2, body2 = _from_container(audit, "GET", "http://helios-core:9000/tools", bearer=secret)
    try:
        count = len(json.loads(body2).get("tools", [])) if status2 == 200 else 0
    except json.JSONDecodeError:
        count = 0
    check("T4.2", status == 401 and status2 == 200 and count >= 30, f"no bearer={status} bearer={status2} tools={count}")
    status, body = _from_container(audit, "POST", "http://helios-core:9000/call", bearer=secret,
                                   body={"tool": "payments.send", "args": {"payer": "operating", "payee": "sup-1",
                                                                            "amount_cents": 1000, "currency": "CNY"}})
    try:
        env = json.loads(body)
    except json.JSONDecodeError:
        env = {}
    check("T4.3", status == 200 and env.get("rejected") is True and "auth" in str(env.get("error", "")).lower(),
          f"envelope={ {k: env.get(k) for k in ('ok', 'rejected', 'unavailable')} } error={str(env.get('error', ''))[:80]}")
    status, body = _from_container(audit, "POST", "http://helios-core:9000/call", bearer=secret,
                                   body={"tool": "graph.publish_verified", "args": {"kind": "demo", "key": "t4", "attrs": {"ok": True}, "source": "demo-check"}})
    _docker("restart", f"helios-core-{t}")
    time.sleep(4)
    status2, body2 = _from_container(audit, "GET", "http://helios-core:9000/kg/nodes", bearer=secret)
    check("T4.5", status == 200 and status2 == 200 and "demo:t4" in body2, f"publish={status} after restart nodes has demo:t4={'demo:t4' in body2}")
    status, body, _ = http("GET", f"{_engine(args)}/api/v1/tenants/{t}/logs")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    apps = payload.get("apps", {})
    chains = [f.get("chain", {}).get("intact") for a in apps.values() for f in a.get("files", []) if f.get("chain")]
    check("T4.6", status == 200 and {"helios-collect", "helios-payables", "helios-cash-planner"} <= set(apps) and chains and all(chains),
          f"apps={sorted(apps)} chains={len(chains)} intact={all(chains) if chains else None}")
    status, _ = _from_container(appc, "GET", "http://helios-core:9000/tools")
    check("T4.7", status in (401, 0), f"app container -> core /tools: {status}")
    # the sample-app moments, redeployed for this rung
    sample = REPO / "platform" / "deployment" / "examples" / "sample-app"
    r = _deploy(args, sample, t)
    slug = _slug("sample-collect", t)
    _wait_page(slug)
    s1, b1, _ = http("POST", f"http://localhost/apps/{slug}/demo/send-reminder", body={})
    s2, b2, _ = http("POST", f"http://localhost/apps/{slug}/demo/send-reminder", body={})
    s3, b3, _ = http("POST", f"http://localhost/apps/{slug}/demo/collect-payment", body={})
    check("T4.8", '"executed"' in b1 and "duplicate" in b2 and '"denied"' in b3 and "auth" in b3.lower(),
          f"email={b1[:60]} | repeat={b2[:60]} | payment={b3[:80]}")


# ---------------------------------------------------------------- T5
def t5(args) -> None:
    t = args.tenant
    cp = _slug("helios-cash-planner", t)
    co = _slug("helios-collect", t)
    pa = _slug("helios-payables", t)
    _page(cp, "/")  # build the planner so it subscribes to the bus
    time.sleep(2)
    before = json.loads(_page(cp, "/events")[1]).get("events", [])
    status, _, _ = http_noredirect("POST", f"http://localhost/apps/{co}/runs", form={"invoice_id": "INV-1005"})
    time.sleep(3)
    events = json.loads(_page(cp, "/events")[1]).get("events", [])
    names = [e.get("event") for e in events[len(before):]]
    check("T5.1", status in (200, 303, 302) and "invoice.extension_agreed" in names, f"collect run={status} planner events={names}")
    diag = json.loads(_page(cp, "/diagnostics")[1])
    check("T5.2", diag.get("events_applied", 0) >= 1, f"planner events_applied={diag.get('events_applied')} first_short_week={diag.get('first_short_week')}")
    status, _, _ = http_noredirect("POST", f"http://localhost/apps/{pa}/run", form={"scope": "all"})
    _, holds = _page(pa, "/holds")
    check("T5.3", status in (303, 302) and "payroll" in holds, f"payables run={status} holds page mentions payroll={'payroll' in holds}")
    r = run(["grep", "-rln", "-E", "helios-collect-|helios-payables-|helios-cash-planner-", "apps/helios-collect/server.py",
             "apps/helios-collect/tools.py", "apps/helios-payables/server.py", "apps/helios-payables/tools.py",
             "apps/helios-cash-planner/server.py", "apps/helios-cash-planner/tools.py"])
    check("T5.4", r.stdout.strip() == "", "no app names another app's URL" if not r.stdout.strip() else r.stdout.strip())


# ---------------------------------------------------------------- T6
def t6(args) -> None:
    t = args.tenant
    work = Path(tempfile.mkdtemp(prefix="helios-t6-")) / "expense-approver"
    spec = work.parent / "spec.yaml"
    work.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text('''spec_version: "1"
identity: { id: expense-approver, name: Expense Approver, description: Approves employee expense claims against a monthly budget per department and reimburses the approved ones. }
stack: { use: helios-python-web-1 }
data:
  local:
    entities:
      - name: claim
        fields:
          - { name: id, type: string, required: true }
          - { name: employee_id, type: string, required: true }
          - { name: department, type: string, required: true }
          - { name: amount, type: decimal, required: true }
          - { name: status, type: string, required: true }
      - name: budget
        fields:
          - { name: id, type: string, required: true }
          - { name: department, type: string, required: true }
          - { name: remaining, type: decimal, required: true }
agents:
  action_manifest: agents/expense-approver-action.json
  audit_manifest: agents/expense-approver-audit.yaml
ui:
  pages:
    - { id: dashboard, title: Claims this month, pattern: summary }
    - { id: claims, title: Claims, pattern: table }
    - { id: holds, title: Needs your approval, pattern: approval_queue }
tools:
  - claims_lookup
  - budgets_remaining
  - { name: approve_claim, side_effect: true }
  - reimbursements_send
''', encoding="utf-8")
    r = _scaffold(work, spec)
    check("T6.1", r.returncode == 0, (r.stdout.strip().splitlines() or [r.stderr[-120:]])[0])
    r = run([REPO / ".venv" / "bin" / "helios-app", "validate", work])
    last = (r.stdout.strip().splitlines() or [""])[-1]
    check("T6.2", r.returncode == 0 and "0 error(s)" in last, last)
    r = _deploy(args, work, t)
    slug = _slug("expense-approver", t)
    ok = r.returncode == 0 and _wait_page(slug) == 200
    check("T6.3", ok, f"deployed /health 200, / {_page(slug, '/')[0]}" if ok else (r.stderr or r.stdout)[-240:])
    run([PY, HELIOS, "undeploy", "expense-approver", "--tenant", t, "--engine", _engine(args)])
    shutil.rmtree(work.parent, ignore_errors=True)


RUNGS = {"T0": t0, "T1": t1, "T2": t2, "T3": t3, "T4": t4, "T5": t5, "T6": t6}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rungs", nargs="+", help="T0..T6 or all")
    ap.add_argument("--tenant", default="demo")
    ap.add_argument("--engine", default=os.environ.get("HELIOS_ENGINE_URL", "http://127.0.0.1:8100"))
    args = ap.parse_args(argv)
    wanted = list(RUNGS) if "all" in args.rungs else [r.upper() for r in args.rungs]
    for rung in wanted:
        if rung not in RUNGS:
            print(f"unknown rung {rung}; choose from {', '.join(RUNGS)} or all", file=sys.stderr)
            return 2
    started = time.time()
    for rung in wanted:
        print(f"== {rung}", flush=True)
        try:
            RUNGS[rung](args)
        except Exception as exc:  # noqa: BLE001 - a crashed rung is a FAIL, not a traceback
            check(f"{rung}.x", False, f"rung crashed: {type(exc).__name__}: {exc}")
    failed = [r for r in RESULTS if not r[1]]
    print(f"== {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed in {time.time() - started:.0f}s"
          + (f"; FAILED: {', '.join(r[0] for r in failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
