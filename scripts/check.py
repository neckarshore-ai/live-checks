#!/usr/bin/env python3
"""check.py — run one live-check group and report it to healthchecks.

Usage:
    python3 scripts/check.py checks/<group>.toml [--probe] [--no-tls]

Environment:
    HC_PING_URL                the group's healthchecks ping URL (a repository
                               secret). Unset: the result is printed, nothing is
                               sent, and the exit code still reports it.
    LIVE_CHECKS_REQUIRE_HTTPS  "1" refuses any non-https URL (set in CI).

Exit codes:
    0  every active assertion passed, success ping sent (if a ping URL is set)
    1  at least one assertion failed; /fail ping sent with the names and URLs
    2  configuration error, or the group has no active assertion — in that
       case NOTHING is sent, because a group that checks nothing must never
       report green (planning #1954)
    3  the ping itself could not be delivered

Public-log rule (AD-65 ruling 1a): this script never prints or sends a
response body. What leaves the run is the assertion name, its public URL, the
HTTP status and the kind of failure — all of which are in the group file
already.
"""
import argparse
import datetime
import os
import socket
import ssl
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "neckarshore-live-checks (+https://github.com/neckarshore-ai/live-checks)"
FETCH_TIMEOUT_S = 20
MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_TLS_MIN_DAYS = 14
PROBE_TEXT = "__live-checks-probe__ this text is never on any page"


class ConfigError(Exception):
    pass


def load_group(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    group = data.get("group")
    if not group:
        raise ConfigError("missing 'group'")
    asserts = data.get("assert", [])
    names = set()
    for a in asserts:
        name = a.get("name")
        if not name:
            raise ConfigError("an assertion has no name")
        if name in names:
            raise ConfigError(f"duplicate assertion name: {name}")
        names.add(name)
        url = a.get("url", "")
        if not url.startswith(("https://", "http://")):
            raise ConfigError(f"{name}: url must be http(s)")
        if os.environ.get("LIVE_CHECKS_REQUIRE_HTTPS") == "1" and not url.startswith("https://"):
            raise ConfigError(f"{name}: plain http is refused in live groups")
        active = a.get("active", True)
        if not active and not str(a.get("reason", "")).strip():
            raise ConfigError(f"{name}: an inactive assertion must name its reason")
        if active and not a.get("expect"):
            raise ConfigError(f"{name}: an active assertion needs at least one expected text")
        a.setdefault("active", True)
        a.setdefault("expect", [])
    data["assert"] = asserts
    return data


def fetch(url):
    """Return (status, body_text). Error statuses still return their body:
    the assertion is on content, not on the code (AD-65)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            return resp.status, resp.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(MAX_BODY_BYTES).decode("utf-8", "replace")


def tls_days_left(host, port=443):
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=FETCH_TIMEOUT_S) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            not_after = tls.getpeercert()["notAfter"]
    expires = ssl.cert_time_to_seconds(not_after)
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    return int((expires - now) // 86400)


def run_assertion(a, probe):
    """Return None on pass, or a short failure kind (never the body)."""
    expect = list(a["expect"]) + ([PROBE_TEXT] if probe else [])
    try:
        status, body = fetch(a["url"])
    except Exception as e:  # noqa: BLE001 — any transport failure is a failure
        return f"unreachable ({type(e).__name__})"
    missing = [t for t in expect if t not in body]
    if missing:
        what = "probe text" if missing == [PROBE_TEXT] else f"{len(missing)} expected text(s) missing"
        return f"{what} (HTTP {status})"
    return None


def ping(ping_url, suffix, body):
    data = body.encode()
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(ping_url + suffix, data=data, method="POST",
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
    print(f"::error::ping{suffix or ''} not delivered ({last})")
    return False


def main_for(path, ping_url, probe=False, tls=True):
    try:
        group = load_group(path)
    except (ConfigError, tomllib.TOMLDecodeError, OSError) as e:
        print(f"::error::config: {e}")
        return 2

    active = [a for a in group["assert"] if a["active"]]
    for a in group["assert"]:
        if not a["active"]:
            print(f"SKIP {a['name']}: {a['reason']}")
    if not active:
        print(f"::error::group {group['group']} has no active assertion — refusing to report green")
        return 2

    failures = []
    for i, a in enumerate(active):
        kind = run_assertion(a, probe=probe and i == 0)
        if kind:
            failures.append(f"FAIL {a['name']} — {a['url']} — {kind}")
            print(f"::error::FAIL {a['name']} — {a['url']} — {kind}")
        else:
            print(f"PASS {a['name']} — {a['url']}")

    if tls:
        hosts = {}
        for a in active:
            u = urllib.parse.urlsplit(a["url"])
            if u.scheme == "https":
                hosts.setdefault(u.hostname, (a, a.get("tls_min_days", DEFAULT_TLS_MIN_DAYS)))
        for host, (a, min_days) in sorted(hosts.items()):
            try:
                days = tls_days_left(host)
            except Exception as e:  # noqa: BLE001
                failures.append(f"FAIL tls {host} — https://{host}/ — handshake failed ({type(e).__name__})")
                print(f"::error::FAIL tls {host} — handshake failed ({type(e).__name__})")
                continue
            if days < min_days:
                failures.append(f"FAIL tls {host} — https://{host}/ — certificate expires in {days} days")
                print(f"::error::FAIL tls {host} — certificate expires in {days} days (< {min_days})")
            else:
                print(f"PASS tls {host} — {days} days left")

    summary = f"{group['group']}: {len(active) - len([f for f in failures if not f.startswith('FAIL tls')])}/{len(active)} assertions passed"
    print(summary)

    if not ping_url:
        print("no HC_PING_URL set — nothing sent")
        return 1 if failures else 0
    if failures:
        delivered = ping(ping_url, "/fail", "\n".join(failures) + ("\n(probe run)" if probe else ""))
        return 1 if delivered else 3
    return 0 if ping(ping_url, "", summary) else 3


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("group_file")
    p.add_argument("--probe", action="store_true",
                   help="add a text no page carries to the first assertion, to prove the alarm path")
    p.add_argument("--no-tls", action="store_true", help="skip the certificate check")
    args = p.parse_args()
    return main_for(args.group_file, ping_url=os.environ.get("HC_PING_URL") or None,
                    probe=args.probe, tls=not args.no_tls)


if __name__ == "__main__":
    sys.exit(main())
