# AGENT-DEPLOYMENT.md — deploy the whole Helios platform on a Mac mini

You are a coding agent with a shell on a Mac mini. Follow this file top to bottom
and the platform, its per-tenant engines and the three flagship applications end
up running and reachable. Every step has a check; do not continue past a failed
check, fix it using the recovery notes, then re-run the check. Work in English.
Do not edit source files; the codebase is frozen for submission. Configuration
lives in environment variables and in files under `.helios-run/`, which is
gitignored.

Final state, so you know what you are aiming for:

| Component | Where | Check |
|---|---|---|
| Python venv with every platform package | `<repo>/.venv` | `scripts/test-all.sh` ends with `all 14 suites green` |
| Docker daemon | Docker Desktop or Colima | `docker info` exits 0 |
| nginx ingress | container `helios-nginx`, port 80 | `curl -s localhost/` answers |
| Deployment engine | `127.0.0.1:8100` | `curl -s 127.0.0.1:8100/health` → `"status":"ok"` |
| Tenant `demo` infrastructure | `helios-core-demo` (tool service), `helios-redis-demo` | created by the first deploy |
| Helios Collect | `http://localhost/apps/helios-collect-demo/` | `/health` 200 |
| Helios Payables | `http://localhost/apps/helios-payables-demo/` | `/health` 200 |
| Helios Cash Planner | `http://localhost/apps/helios-cash-planner-demo/` | `/health` 200 |
| Sample app (platform path demo) | `http://localhost/apps/sample-collect-demo/` | `/health` 200 |

Reachable from another machine on the same network at `http://<mac-mini-ip>/apps/<app>-demo/`.

---

## 0. Where to run

```bash
cd <the cloned repository>          # the directory that contains platform/, apps/, scripts/
git branch --show-current           # expect: integration (or the submitted branch)
ls scripts platform apps            # expect: bootstrap-venv.sh test-all.sh demo-check.sh ...
```

Everything below is run from this directory unless a step says otherwise.

## 1. Prerequisites: Homebrew, Python 3.13, Docker

```bash
xcode-select -p >/dev/null 2>&1 || xcode-select --install
command -v brew >/dev/null || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || eval "$(/usr/local/bin/brew shellenv)"

brew install python@3.13
python3.13 --version                # check: Python 3.13.x
```

Docker. Prefer Docker Desktop if it is installed; otherwise Colima:

```bash
if [ -d /Applications/Docker.app ]; then
  open -a Docker
  for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
else
  brew install docker colima
  colima start --cpu 4 --memory 6 --disk 60
fi
docker info >/dev/null 2>&1 && echo DOCKER_OK   # check: DOCKER_OK
```

Recovery: if `docker info` still fails with Colima, run `colima status`; if it
says stopped, `colima start`. With Docker Desktop, wait for the whale icon to
stop animating, then re-run the check. If `DOCKER_HOST` is set in the shell to a
socket that does not exist, `unset DOCKER_HOST`.

## 2. Base images (Docker Hub may be unreachable here)

The golden images are built `FROM python:3.13-slim-bookworm`; nginx and Redis are
`nginx:alpine` and `redis:7-alpine`. Pull them now so a build never waits on the
registry:

```bash
for img in python:3.13-slim-bookworm nginx:alpine redis:7-alpine; do
  docker pull "$img" || {
    for mirror in docker.m.daocloud.io dockerproxy.net; do
      docker pull "$mirror/library/$img" && docker tag "$mirror/library/$img" "$img" && break
    done
  }
done
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^(python:3.13-slim-bookworm|nginx:alpine|redis:7-alpine)$' | wc -l   # check: 3
```

Recovery: if the count is below 3, try the other mirror, or
`docker pull registry.cn-hangzhou.aliyuncs.com/library/<img>` and retag. Do not
continue until all three images are present under their canonical names.

## 3. Python environment and the offline test suite

```bash
scripts/bootstrap-venv.sh           # creates .venv on 3.13, installs six packages editable + pinned deps
.venv/bin/python -c "import agent_toolkit, audit_agent, agent_integration, fin_tools, engine, app_builder; print('IMPORTS_OK')"
scripts/test-all.sh | tail -1       # check: all 14 suites green: <N> passed
```

Recovery: if bootstrap fails to find Python 3.13, run it as
`PYTHON313=$(command -v python3.13) scripts/bootstrap-venv.sh`. If PyPI is
unreachable, set a mirror: `.venv/bin/pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`
and re-run bootstrap. The suite needs no network; a failing suite here means the
environment, not the network.

## 4. Start the platform: nginx and the deployment engine

```bash
( cd platform/deployment && ./bootstrap.sh ) > /tmp/helios-bootstrap.log 2>&1
tail -5 /tmp/helios-bootstrap.log
curl -s 127.0.0.1:8100/health        # check: {"status":"ok","service":"helios-deployment-engine",...}
curl -s 127.0.0.1:8100/api/v1/doctor | head -c 300   # check: "engine":"ok" and "docker"
docker ps --format '{{.Names}}' | grep -x helios-nginx   # check: helios-nginx
```

The script detects the daemon socket, creates the ingress network, starts
`helios-nginx` on port 80 mounting `<repo>/.helios-run/nginx`, and starts the
engine as a background `uvicorn` on `127.0.0.1:8100` logging to
`platform/deployment/engine.log`. It is idempotent: re-run it any time.

Recovery:
- Engine not answering: `tail -30 platform/deployment/engine.log`. If the port is
  held by something else, `HELIOS_ENGINE_PORT=8101 ./bootstrap.sh` and add
  `--engine http://127.0.0.1:8101` to every `helios.py` command below.
- To start the engine by hand:
  `cd platform/deployment && HELIOS_REPO_ROOT="$(cd ../.. && pwd)" nohup ../../.venv/bin/python -m uvicorn engine.app:app --host 127.0.0.1 --port 8100 > engine.log 2>&1 < /dev/null &`
- nginx missing: `docker rm -f helios-nginx` then re-run `bootstrap.sh`.

## 5. Deploy the applications

Set the CLI alias, then deploy the sample app first (it builds the three golden
images, which takes several minutes the first time) and the three flagships.

```bash
HELIOS=".venv/bin/python platform/deployment/cli/helios.py"
$HELIOS doctor                                                     # check: exit 0

$HELIOS deploy platform/deployment/examples/sample-app --tenant demo   # builds images; expect "status": "deployed"
$HELIOS deploy apps/helios-collect      --tenant demo
$HELIOS deploy apps/helios-payables     --tenant demo
$HELIOS deploy apps/helios-cash-planner --tenant demo
$HELIOS apps                                                       # check: four app containers, four audit sidecars, running
```

Each deploy prints a JSON record ending with the URLs. Exit codes: `0` deployed;
`1` rejected, the output lists every problem and, for build or smoke failures,
the container log; `2` a `.env` file is inside the app directory (remove it);
`3` the engine is unreachable (go back to step 4); `4` usage.

Recovery:
- `stage: base-image` with a pull error: step 2 was skipped; pull and retag, then redeploy.
- `stage: smoke_test`: read the `logs` field. The flagships in this repository
  pass it; if one fails here on this machine, run
  `docker logs $(docker ps -a --format '{{.Names}}' | grep smoke | head -1)` and report the traceback.
- A redeploy of the same app is safe: the previous version keeps serving until
  the new one is healthy.

## 6. Verify every application

```bash
for app in sample-collect helios-collect helios-payables helios-cash-planner; do
  printf '%-22s health=%s root=%s\n' "$app" \
    "$(curl -s -o /dev/null -w '%{http_code}' http://localhost/apps/$app-demo/health)" \
    "$(curl -s -o /dev/null -w '%{http_code}' http://localhost/apps/$app-demo/)"
done
# check: every line shows health=200 root=200
```

Then run the scripted ladder for the deployed platform (deploy, tenant core,
cross-app). It redeploys the sample app and Collect as part of its checks and
prints one PASS/FAIL line per check:

```bash
scripts/demo-check.sh T3 T4 T5 --tenant demo        # check: last line "N/N checks passed"
```

If a single check fails, read its observed value on that line, apply the matching
recovery note above, and re-run only that rung (`T3`, `T4` or `T5`).

## 7. Exercise the demo moments (optional, for a rehearsal)

Each app has a "Run the agent" button on its dashboard and an approval queue at
`/holds`. From the shell:

```bash
curl -s -X POST -d 'invoice_id=INV-1003' http://localhost/apps/helios-collect-demo/runs -o /dev/null -w '%{http_code}\n'   # 303
curl -s -X POST -d 'scope=all'           http://localhost/apps/helios-payables-demo/run  -o /dev/null -w '%{http_code}\n'   # 303
curl -s -X POST                          http://localhost/apps/helios-cash-planner-demo/run -o /dev/null -w '%{http_code}\n' # 303
$HELIOS evidence --tenant demo              # every app's action, audit and sidecar logs, chains intact
```

Judge moments: Collect INV-1006 and INV-1009; Payables BILL-2002, BILL-2012,
BILL-2016; Cash Planner WARN-INVENTED, WARN-INFLATED, DRAW. Their meaning is in
each app's `README.md`.

## 8. Expose to other machines

nginx listens on `0.0.0.0:80`, so the apps are reachable at
`http://<mac-mini-ip>/apps/<app>-demo/`. Find the address with
`ipconfig getifaddr en0` (Wi-Fi) or `en1`. The engine itself stays on loopback on
purpose; deploy from this machine.

## 9. Optional environment

Export before step 5 to change behaviour; none is required:

| Variable | Effect |
|---|---|
| `TUSHARE_TOKEN` | the tenant core answers market data live instead of from fixtures |
| `DASHSCOPE_API_KEY` | lets an app's model driver run Qwen live; demos use the scripted driver |
| `HELIOS_ENGINE_PORT` | engine port if 8100 is taken |

## 10. Reset or tear down

```bash
$HELIOS undeploy demo                    # removes every container of tenant demo; data volumes are kept
docker rm -f helios-nginx                # only if nginx must be recreated
pkill -f "uvicorn engine.app:app"        # stops the engine
```

To wipe tenant state as well: `rm -rf .helios-run` and
`docker volume rm $(docker volume ls -q | grep '^helios-data-')`, then start again
from step 4.

## Done when

- `curl -s 127.0.0.1:8100/health` reports ok;
- `$HELIOS apps` lists `demo-helios-collect-app`, `demo-helios-payables-app`,
  `demo-helios-cash-planner-app`, `demo-sample-collect-app` and their audit sidecars as running;
- the loop in step 6 prints `health=200 root=200` for all four;
- `scripts/demo-check.sh T3 T4 T5` reports every check passed.

Report those four results verbatim.
