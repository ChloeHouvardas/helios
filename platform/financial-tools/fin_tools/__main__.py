"""Runnable tool service: `python -m fin_tools`.

This is the deployment artifact — the thing that goes in a container so the
platform owns the API and agents call it across a boundary
(specs/financial-tools-service.md §2).

    export HELIOS_INTENT_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
    python -m fin_tools --port 8080

The signing secret is read from the environment only. It is never a command-line
argument (visible in process listings), never hard-coded, and never returned to a
client. Without it the service starts with a development secret and says so
loudly, because a production deployment running unsigned authorizations is the
failure mode worth making obvious.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import build_world
from .service import EXECUTION_SECRET_ENV, SECRET_ENV, ToolServer
from .persistence import DATA_DIR_ENV, SqlitePersistence
from .toolkit_bridge import NOT_AGENT_CALLABLE

_DEV_SECRET = b"dev-only-secret-rotate-me"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fin_tools",
        description="Serve the Helios financial tools over HTTP.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1; use 0.0.0.0 in a container)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    secret = os.environ.get(SECRET_ENV, "").encode() or None
    execution_secret = os.environ.get(EXECUTION_SECRET_ENV) or None
    data_dir = os.environ.get(DATA_DIR_ENV) or None

    bundle = build_world(data_dir=data_dir)

    # Durability: restore the last snapshot before serving, then persist after
    # each mutating call. A no-op when HELIOS_DATA_DIR is unset (tests, local dev).
    persistence = SqlitePersistence(data_dir=data_dir)
    restored = persistence.restore(bundle)

    server = ToolServer(bundle, host=args.host, port=args.port, secret=secret,
                        execution_secret=execution_secret, persistence=persistence)

    if not secret:
        print(f"WARNING: {SECRET_ENV} is not set; running with a development "
              f"secret. Authorizations are forgeable by anyone holding this "
              f"code. Set it before deploying.", file=sys.stderr)
    if not execution_secret:
        print(f"WARNING: {EXECUTION_SECRET_ENV} is not set; the HTTP endpoint is "
              f"UNAUTHENTICATED. Any caller who can reach this port can invoke "
              f"the tools. Set it before deploying.", file=sys.stderr)

    agent_visible = [t.name for t in bundle.registry.list()
                     if t.name not in NOT_AGENT_CALLABLE]
    gated = [t.name for t in bundle.registry.list()
             if t.side_effects and t.name not in NOT_AGENT_CALLABLE]

    server.start()
    print(f"financial tools service on {server.base_url}")
    print(f"  tools published:      {len(agent_visible)}")
    print(f"  gated (need review):  {len(gated)} -> {', '.join(sorted(gated))}")
    print(f"  intent signing:       {'from ' + SECRET_ENV if secret else 'DEVELOPMENT SECRET'}")
    print(f"  http auth:            {'bearer (' + EXECUTION_SECRET_ENV + ')' if execution_secret else 'NONE'}")
    print(f"  persistence:          {'restored from ' + str(persistence.path) if restored else (str(persistence.path) if persistence.enabled else 'in-memory only')}")
    print(f"  discover:  curl {server.base_url}/tools")
    print("  press Ctrl-C to stop")
    try:
        server._thread.join()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.stop()
        persistence.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
