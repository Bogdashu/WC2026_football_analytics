#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wc2026_check_api.py — diagnose why football-data.org is unreachable.

RUN THIS WHERE THE BOT RUNS (Railway shell), because the problem is network-path
specific: the same request can work from your laptop and fail from a datacenter IP.

  python -X utf8 wc2026_check_api.py

It checks, step by step:
  1. DNS resolution of api.football-data.org
  2. TCP + TLS handshake (and negotiated TLS version)
  3. The real GET /v4/competitions/WC/matches?season=2026 (with your API key)
  4. The same without the season filter
and prints a clear verdict + the rate-limit headers football-data.org returns.
"""
import os, sys, ssl, json, time, socket
import http.client
import urllib.request, urllib.error

HOST = "api.football-data.org"
API_BASE = "https://api.football-data.org/v4"
COMP = "WC"
KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()


def line():
    print("-" * 60)


def step_dns():
    line(); print("1) DNS resolve", HOST)
    try:
        infos = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        print("   OK ->", ", ".join(ips))
        return True
    except Exception as e:
        print("   DNS FAILED:", repr(e))
        print("   => The container cannot resolve the host (DNS/egress problem).")
        return False


def step_tls():
    line(); print("2) TCP + TLS handshake to", HOST + ":443")
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((HOST, 443), timeout=20) as sock:
            with ctx.wrap_socket(sock, server_hostname=HOST) as ss:
                print("   OK  TLS", ss.version(), "cipher", ss.cipher()[0])
        return True
    except Exception as e:
        print("   TLS/TCP FAILED:", repr(e))
        print("   => Connection blocked/reset before HTTP (firewall/WAF/IP block).")
        return False


def step_http(path, label):
    line(); print(f"{label}) GET {API_BASE}{path}")
    headers = {
        "X-Auth-Token": KEY,
        "User-Agent": "WC2026-bot/1.0 (+https://github.com/Bogdashu/WC2026_football_analytics)",
        "Accept": "application/json",
        "Connection": "close",
    }
    t0 = time.time()
    try:
        req = urllib.request.Request(API_BASE + path, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            dt = time.time() - t0
            body = resp.read()
            print(f"   HTTP {resp.status} in {dt:.1f}s, {len(body)} bytes")
            for h in ("X-Requests-Available-Minute", "X-RequestCounter-Reset",
                      "X-Requests-Available", "Retry-After"):
                v = resp.headers.get(h)
                if v is not None:
                    print(f"   {h}: {v}")
            try:
                data = json.loads(body)
                ms = data.get("matches", [])
                fin = [m for m in ms if m.get("status") == "FINISHED"]
                print(f"   matches={len(ms)}  finished={len(fin)}")
            except Exception:
                print("   (body is not JSON)", body[:200])
            return True
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        print(f"   HTTP ERROR {e.code} {e.reason} in {dt:.1f}s")
        try:
            print("   body:", e.read()[:300])
        except Exception:
            pass
        if e.code == 403:
            print("   => Key not authorized for this competition/filter on the free tier.")
        elif e.code == 429:
            print("   => Rate limited (free tier = 10 req/min). Wait and retry.")
        return False
    except (urllib.error.URLError, http.client.RemoteDisconnected,
            http.client.IncompleteRead, ConnectionError, socket.timeout, OSError) as e:
        dt = time.time() - t0
        print(f"   CONNECTION DROPPED after {dt:.1f}s: {e!r}")
        print("   => Server accepted the socket but closed it without a response.")
        print("      Most often: temporary outage OR Cloudflare resetting a datacenter IP.")
        return False


def main():
    print("football-data.org connectivity diagnostic")
    print("API key present:", "yes" if KEY else "NO (set FOOTBALL_DATA_API_KEY)")
    dns_ok = step_dns()
    tls_ok = step_tls() if dns_ok else False
    if KEY and tls_ok:
        ok1 = step_http(f"/competitions/{COMP}/matches?season=2026", "3")
        ok2 = step_http(f"/competitions/{COMP}/matches", "4")
    else:
        ok1 = ok2 = False
        if not KEY:
            line(); print("Skipping HTTP checks: no API key in env.")
    line()
    print("VERDICT:")
    if not dns_ok:
        print("  DNS fails -> container egress/DNS issue (not football-data.org).")
    elif not tls_ok:
        print("  TLS/TCP blocked -> firewall/WAF/IP block on the network path.")
    elif ok1 or ok2:
        print("  API reachable now. If /update still fails, the earlier drop was transient.")
    else:
        print("  Host reachable at TLS level but HTTP drops/errs -> likely temporary")
        print("  outage or Cloudflare blocking this IP. Re-run in a few minutes; if it")
        print("  persists for hours, the Railway egress IP is probably being reset.")


if __name__ == "__main__":
    main()
