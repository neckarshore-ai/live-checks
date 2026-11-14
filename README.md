# live-checks

Scheduled availability checks for the public surfaces of Neckarshore AI. One
group per workflow; each group asserts page **content** (not the status code)
and certificate lifetime, and reports to its own healthchecks row. A failed
assertion sends that row's `/fail` ping with the assertion name and the public
URL; a run that never happens shows up as a lapsed ping.

This repository is public on purpose: scheduled runs on GitHub-hosted runners
are free here, and every URL checked is public already. Nothing private goes
in — no form payloads, no secret values, no response bodies in the logs. The
ping URLs live in repository secrets. Issues are disabled; the alarm path is
healthchecks.

## Groups

| # | Group | Owner | State |
|---|-------|-------|-------|
| 1 | `forms-live` | Linus | skeleton, no schedule |
| 2 | `kaze-relay-live` | Ozzy | skeleton, no schedule (P1 once armed) |
| 3 | `web-mmps-live` | Bob | hourly |
| 4 | `websites-live` | Linus | skeleton, no schedule |

## Adding an assertion

Add an `[[assert]]` entry to `checks/<group>.toml`: a name, an `https://` URL
and at least one `expect` text that only the real page carries. An entry that
is known but not checked carries `active = false` and a `reason` that names
what turns it back on. A group without an active assertion never reports
green — the runner exits 2 and sends nothing.

## Running locally

```bash
python3 -m unittest discover -s tests -v
python3 scripts/check.py checks/web-mmps-live.toml          # no ping without HC_PING_URL
python3 scripts/check.py checks/web-mmps-live.toml --probe  # proves the /fail path
```

## Background

Decision record: AD-65 in the Neckarshore planning repository (estate
monitoring), work order planning #4005.
