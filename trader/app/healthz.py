"""コンテナ healthcheck 用。指定サービスのハートビート鮮度を見て exit code を返す。

  usage: python healthz.py <service> [stale_sec]
  fresh -> exit 0 / stale or missing -> exit 1
docker-compose の healthcheck から各 app サービス（risk/executor/strategy/monitor）が使う。
"""
from __future__ import annotations

import sys
import time

import common


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: healthz.py <service> [stale_sec]")
        return 2
    service = sys.argv[1]
    stale = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    beats = common.read_heartbeats()
    ts = beats.get(service)
    if ts is None or (time.time() - ts) > stale:
        print(f"{service}: stale/missing")
        return 1
    print(f"{service}: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
