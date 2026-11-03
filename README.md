# Mist Client Export

Exports clients seen in a Mist organization during the last X days to a CSV
file, using the Mist API Python library (`mistapi`). All result pages are
retrieved.

## Setup

Python 3.10 or newer is required.

```powershell
py -m pip install -r requirements.txt
```

MistAPI prompts for authentication when credentials are not configured. For
repeat or unattended use, create a `.env` file (do not commit it):

```
MIST_HOST=api.mist.com
MIST_APITOKEN=your_token_here
```

## Run

```powershell
py wifi_clients_last_days.py                         # prompts for days and org
py wifi_clients_last_days.py --days 7
py wifi_clients_last_days.py -d 3 --type all --org-id <org_id> -e .env -o clients.csv
```

| Option | Meaning |
| --- | --- |
| `-d`, `--days` | Days to look back, 1–30 (prompted if omitted) |
| `-t`, `--type` | `wireless` (default), `wired`, or `all` |
| `--org-id` | Org ID; if omitted you pick from a list |
| `-e`, `--env-file` | `.env` file with Mist credentials |
| `--token` | Mist API token; skips email/password login (default: `MIST_APITOKEN` env var) |
| `--host` | Mist API host, e.g. `api.eu.mist.com` (default: `MIST_HOST` env var; `api.mist.com` for token auth) |
| `--login` | Ignore any API token and log in with email/password, so every org on your account is listed |

### Which login is used

1. `--login` given → email/password (uses `MIST_USER` / `MIST_PASSWORD` if set,
   otherwise prompts; 2FA prompts if enabled).
2. A token is found (`--token`, `MIST_APITOKEN`, or `.env`) → token login, no prompts.
   An **org** API token only sees its own org, so the org list has one entry.
3. Otherwise → email/password, as in 1.

`--org-id` without `--login` must not prompt, so it needs either a token or both
`MIST_USER` and `MIST_PASSWORD`; if neither is set the script exits with an error.
| `-o`, `--output` | CSV path; default is auto-named with org name, type, days and timestamp, e.g. `Acme-Corp_wireless_clients_last_7_days_20261103_185301.csv` |

### Fully non-interactive (API token + org ID)

```powershell
$env:MIST_APITOKEN = "your_token_here"
py wifi_clients_last_days.py -d 7 --org-id <org_id>
```

Supplying `--token` (or `MIST_APITOKEN`) with `--org-id` and `--days` runs with no
prompts. A `.env` file in the current folder or the script folder is loaded
automatically, so with one in place `py wifi_clients_last_days.py -d 7 --org-id <org_id>`
is enough. If `--org-id` is given but no token is found, the script exits with an
error instead of prompting for a login. Prefer the environment variable over `--token` so the token is not saved
in your shell history.

## How far back it can search

Mist's client search (`clients/search`) accepts any range but only returns clients
seen in about the last 7–8 days. To cover longer ranges, the script works like this:

- **Wireless, 1–7 days:** client search only.
- **Wireless, 8–30 days:** client search plus wireless session history, which Mist
  keeps for 30 days. Sessions are recorded only once a connection ends, so client
  search is still needed for devices connected right now.
- **Wired:** Mist has no wired session history, so wired results cover about 7–8
  days whatever `--days` is set to. The script prints a note when this applies.

## Progress display

Results are fetched 1,000 at a time. In a terminal, each search shows a live bar:

```
Fetching wireless sessions: [#############-----------------]  44%  2,000/4,500  12s
```

When output is redirected to a file or log, a progress line is printed every 10
pages instead. Progress goes to stderr, so it never mixes into piped output.

## CSV contents

One row per client MAC address. The first columns are:

| Column | Meaning |
| --- | --- |
| `client_type` | `wireless` or `wired` |
| `mac` | Client MAC address |
| `last_seen` | Most recent time seen, in local time |
| `source` | `clients/search`, `sessions`, or `both` |

Clients found only in session history have fewer details (no IP, hostname or
username), because session records don't include them. Nested values are written
as JSON text.
If no clients match, no file is created.
