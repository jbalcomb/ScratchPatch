"""`python -m home_runner serve | key`.

  serve   start the API (default 127.0.0.1:8765; put `tailscale serve` in front of it)
  key     print the access key, creating it first if needed (--new replaces it)
"""

import argparse
import logging
import os
import secrets
import sys
from pathlib import Path

from . import registry
from .runs import Runner
from .server import make_server

PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_KEY_FILE = Path.home() / ".config" / "home-runner" / "key"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="home_runner")
    parser.add_argument("command", choices=["serve", "key"])
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default %(default)s; or the machine's 100.x Tailscale address)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--jobs", type=Path, default=PROJECT / "jobs.toml")
    parser.add_argument("--data", type=Path, default=PROJECT / "data")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--log-file", help="append the log here instead of printing it")
    parser.add_argument("--new", action="store_true", help="key: replace the existing key")
    args = parser.parse_args(argv)

    if args.command == "key":
        print(load_key(args.key_file, replace=args.new))
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        filename=args.log_file, encoding="utf-8")
    key = load_key(args.key_file)
    jobs = registry.load(args.jobs)
    runner = Runner(jobs, args.data)
    server = make_server(runner, key, args.host, args.port)
    logging.info("home-runner on http://%s:%d with %d jobs from %s", args.host, args.port, len(jobs), args.jobs)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def load_key(path, replace=False):
    if replace or not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(24))
    return path.read_text(encoding="utf-8").strip()


if __name__ == "__main__":
    sys.exit(main())
