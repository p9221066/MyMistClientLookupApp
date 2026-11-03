"""Export Mist organization clients seen within a chosen number of days to CSV.

Examples:
    py wifi_clients_last_days.py                      # prompts for days and org
    py wifi_clients_last_days.py --days 30            # wireless: up to 30 days
    py wifi_clients_last_days.py -d 3 --type all --org-id <org_id> -o clients.csv
    py wifi_clients_last_days.py -d 7 --org-id <org_id> --token <api_token>   # no prompts
    py wifi_clients_last_days.py --login              # email/password login, lists all your orgs
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import mistapi
from dotenv import load_dotenv  # installed with mistapi

PAGE_SIZE = 1000  # Mist search endpoints accept up to 1000 results per page
MAX_DAYS = 30  # Mist keeps wireless session history for 30 days
# clients/search accepts any duration but, measured on a live org, only returns
# clients seen in about the last 7-8 days. Beyond that, session history fills the gap.
CLIENT_SEARCH_DAYS = 7


def day_count(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if not 1 <= days <= MAX_DAYS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_DAYS}")
    return days


def load_env_files(explicit: str | None) -> None:
    """Load MIST_* settings. An explicit --env-file wins; otherwise use a .env in the
    current folder or the script folder. Variables already set in the shell are kept."""
    if explicit:
        if not Path(explicit).is_file():
            raise SystemExit(f"Env file not found: {explicit}")
        load_dotenv(explicit, override=True)
        return
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def parse_args() -> argparse.Namespace:
    # Pre-read --env-file so its values can serve as defaults for --token / --host.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("-e", "--env-file")
    load_env_files(pre.parse_known_args()[0].env_file)

    parser = argparse.ArgumentParser(description="Export Mist clients seen in the last X days to CSV.")
    parser.add_argument("-d", "--days", type=day_count, help=f"number of days to look back, 1-{MAX_DAYS} (prompted if omitted)")
    parser.add_argument(
        "-t", "--type", choices=("wireless", "wired", "all"), default="wireless",
        help="which clients to export (default: wireless)",
    )
    parser.add_argument("--org-id", help="Mist org ID (prompted to select if omitted)")
    parser.add_argument("-e", "--env-file", help="path to a .env file with MIST_HOST / MIST_APITOKEN")
    parser.add_argument(
        "--token", default=os.environ.get("MIST_APITOKEN"),
        help="Mist API token; skips interactive login (default: MIST_APITOKEN env var)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("MIST_HOST"),
        help="Mist API host, e.g. api.mist.com, api.eu.mist.com (default: MIST_HOST env var; "
             "api.mist.com for token auth)",
    )
    parser.add_argument(
        "--login", action="store_true",
        help="ignore any API token and log in with email/password, so every org on your account is listed",
    )
    parser.add_argument("-o", "--output", type=Path, help="CSV output path (default: auto-named in current folder)")
    return parser.parse_args()


def prompt_for_days() -> int:
    while True:
        value = input(f"How many days back should be searched (1-{MAX_DAYS})? (or q to quit): ").strip()
        if value.lower() in {"q", "quit"}:
            raise SystemExit("Cancelled.")
        try:
            return day_count(value)
        except argparse.ArgumentTypeError:
            print(f"Enter a whole number from 1 to {MAX_DAYS}.")


class Progress:
    """One-line progress display on stderr. In a terminal it redraws in place as a bar;
    when output is redirected to a file or log, it prints a line every 10 pages instead."""

    BAR_WIDTH = 30

    def __init__(self, label: str, total: int | None) -> None:
        self.label, self.total = label, total or None
        self.start = time.monotonic()
        self.interactive = sys.stderr.isatty()
        self.pages = 0

    def update(self, fetched: int) -> None:
        self.pages += 1
        if not self.interactive and self.pages % 10:
            return
        elapsed = time.monotonic() - self.start
        if self.total:
            share = min(fetched / self.total, 1.0)
            filled = int(share * self.BAR_WIDTH)
            bar = "#" * filled + "-" * (self.BAR_WIDTH - filled)
            text = f"{self.label}: [{bar}] {share:4.0%}  {fetched:,}/{self.total:,}  {elapsed:.0f}s"
        else:
            text = f"{self.label}: {fetched:,} fetched  {elapsed:.0f}s"
        if self.interactive:
            sys.stderr.write("\r" + text.ljust(100)[:100])
        else:
            sys.stderr.write(text + "\n")
        sys.stderr.flush()

    def done(self, fetched: int) -> None:
        elapsed = time.monotonic() - self.start
        if self.interactive:
            sys.stderr.write("\r" + " " * 100 + "\r")
        sys.stderr.write(f"{self.label}: {fetched:,} records in {elapsed:.1f}s\n")
        sys.stderr.flush()


def page_results(response: Any, label: str) -> list[Mapping[str, Any]]:
    if response.status_code != 200:
        raise SystemExit(f"Mist API error ({label}): HTTP {response.status_code} {response.data}")
    data = response.data
    results = data.get("results") if isinstance(data, Mapping) else data
    if not isinstance(results, list) or not all(isinstance(r, Mapping) for r in results):
        raise TypeError(f"MistAPI returned an unexpected {label} response.")
    return results


def run_search(search: Any, apisession: Any, org_id: str, days: int, label: str) -> list[Mapping[str, Any]]:
    """Fetch every page of a Mist search, showing progress as pages arrive."""
    response = search(apisession, org_id, duration=f"{days}d", limit=PAGE_SIZE)
    results = list(page_results(response, label))
    total = response.data.get("total") if isinstance(response.data, Mapping) else None
    progress = Progress(f"Fetching {label}", total if isinstance(total, int) else None)
    progress.update(len(results))

    while response.next:
        next_response = mistapi.get_next(apisession, response)
        if next_response is None:
            raise SystemExit(f"Mist API error ({label}): failed to fetch the next page.")
        response = next_response
        page = page_results(response, label)
        if not page:
            break
        results.extend(page)
        progress.update(len(results))

    progress.done(len(results))
    return results


def format_time(epoch: float | None) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds") if epoch else ""


def clients_by_mac(records: list[Mapping[str, Any]], client_type: str) -> dict[str, dict[str, Any]]:
    """One row per MAC from clients/search, keeping the most recently seen record."""
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        mac = record.get("mac")
        seen = record.get("timestamp") or 0
        if mac and (mac not in merged or seen > merged[mac]["_seen"]):
            merged[mac] = {"client_type": client_type, "mac": mac, "_seen": seen,
                           "source": "clients/search", **record}
    return merged


def add_sessions(merged: dict[str, dict[str, Any]], sessions: list[Mapping[str, Any]]) -> None:
    """Fold wireless session history into the per-MAC rows. Sessions cover 30 days but
    are only recorded once a connection ends, so current clients come from clients/search."""
    for session in sessions:
        mac = session.get("mac")
        if not mac:
            continue
        seen = session.get("disconnect") or session.get("timestamp") or 0
        row = merged.get(mac)
        if row is None:
            merged[mac] = {
                "client_type": "wireless", "mac": mac, "_seen": seen, "source": "sessions",
                "site_id": session.get("site_id"), "ssid": session.get("ssid"),
                "wlan_id": session.get("wlan_id"), "ap": session.get("ap"),
                "band": session.get("band"), "random_mac": session.get("random_mac"),
                "os": session.get("client_os"), "model": session.get("client_model"),
                "mfg": session.get("client_manufacture"), "family": session.get("client_family"),
            }
        else:
            if row["source"] == "clients/search":
                row["source"] = "both"
            if seen > row["_seen"]:
                row["_seen"] = seen
                if row["source"] == "sessions":  # refresh details from the latest session
                    row.update(ssid=session.get("ssid"), ap=session.get("ap"),
                               band=session.get("band"), site_id=session.get("site_id"))


def fetch_clients(apisession: Any, org_id: str, days: int, client_type: str) -> list[dict[str, Any]]:
    if client_type == "wireless":
        records = run_search(mistapi.api.v1.orgs.clients.searchOrgWirelessClients,
                             apisession, org_id, days, "wireless clients")
        merged = clients_by_mac(records, "wireless")
        if days > CLIENT_SEARCH_DAYS:
            sessions = run_search(mistapi.api.v1.orgs.clients.searchOrgWirelessClientSessions,
                                  apisession, org_id, days, "wireless sessions")
            add_sessions(merged, sessions)
    else:
        records = run_search(mistapi.api.v1.orgs.wired_clients.searchOrgWiredClients,
                             apisession, org_id, days, "wired clients")
        merged = clients_by_mac(records, "wired")
        if days > CLIENT_SEARCH_DAYS:
            print(f"Note: Mist has no wired session history, so wired results only cover "
                  f"about the last {CLIENT_SEARCH_DAYS}-8 days.")

    rows = sorted(merged.values(), key=lambda r: r["_seen"], reverse=True)
    for row in rows:
        row["last_seen"] = format_time(row.pop("_seen"))
    return rows


def get_org_name(apisession: Any, org_id: str) -> str | None:
    response = mistapi.api.v1.orgs.orgs.getOrg(apisession, org_id)
    if response.status_code == 200 and isinstance(response.data, Mapping):
        return response.data.get("name")
    return None


def filename_safe(text: str) -> str:
    """Make an org name usable in a filename on any OS: spaces become '-', and
    characters Windows forbids (\\ / : * ? " < > |) are dropped."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text).strip().strip(".")
    return re.sub(r"\s+", "-", cleaned)[:60] or "org"


def export_clients(clients: list[dict[str, Any]], output_path: Path) -> None:
    leading = ["client_type", "mac", "last_seen", "source"]
    fieldnames = list(dict.fromkeys(leading + [key for client in clients for key in client]))
    with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for client in clients:
            writer.writerow({
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                for key, value in client.items()
            })


def main() -> None:
    args = parse_args()
    days = args.days or prompt_for_days()

    if args.token and not args.login:
        # Token auth (from --token, MIST_APITOKEN, or .env): no email/password prompt.
        apisession = mistapi.APISession(apitoken=args.token, host=args.host or "api.mist.com")
    else:
        # Email/password login. mistapi also reads MIST_APITOKEN from the environment,
        # so drop it here or it would silently log in with the token instead.
        os.environ.pop("MIST_APITOKEN", None)
        has_credentials = bool(os.environ.get("MIST_USER") and os.environ.get("MIST_PASSWORD"))
        if args.org_id and not args.login and not has_credentials:
            raise SystemExit(
                "--org-id runs without prompts, so it needs credentials up front: an API token "
                "(--token or MIST_APITOKEN) or MIST_USER + MIST_PASSWORD, in the environment or "
                "a .env file. Add --login to be prompted instead."
            )
        # Uses MIST_USER / MIST_PASSWORD when set; prompts for anything missing (including 2FA).
        apisession = mistapi.APISession(host=args.host)
    apisession.login()
    if not apisession.get_authentication_status():
        raise SystemExit("Mist authentication failed. Check the API token or credentials, and the host.")
    org_id = args.org_id or mistapi.cli.select_org(apisession)[0]

    types = ("wireless", "wired") if args.type == "all" else (args.type,)
    clients: list[dict[str, Any]] = []
    for client_type in types:
        found = fetch_clients(apisession, org_id, days, client_type)
        print(f"Found {len(found)} unique {client_type} clients in the last {days} days.")
        clients.extend(found)

    if not clients:
        print("Nothing to export.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    org_label = filename_safe(get_org_name(apisession, org_id) or org_id)
    output_path = args.output or Path.cwd() / f"{org_label}_{args.type}_clients_last_{days}_days_{timestamp}.csv"
    export_clients(clients, output_path)
    print(f"Exported {len(clients)} clients to {output_path.resolve()}")


if __name__ == "__main__":
    main()
