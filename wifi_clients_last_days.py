"""Export Mist organization clients seen within a chosen number of days to CSV.

Examples:
    py wifi_clients_last_days.py                      # prompts for days and org
    py wifi_clients_last_days.py --days 30            # wireless: up to 30 days
    py wifi_clients_last_days.py -d 3 --type all --org-id <org_id> -o clients.csv
    py wifi_clients_last_days.py -d 7 --org-id <org_id> --token <api_token>   # no prompts
"""

import argparse
import csv
import json
import os
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
        "--host", default=os.environ.get("MIST_HOST", "api.mist.com"),
        help="Mist API host, e.g. api.mist.com, api.eu.mist.com (default: MIST_HOST env var or api.mist.com)",
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


def run_search(search: Any, apisession: Any, org_id: str, days: int, label: str) -> list[Mapping[str, Any]]:
    response = search(apisession, org_id, duration=f"{days}d", limit=PAGE_SIZE)
    if response.status_code != 200:
        raise SystemExit(f"Mist API error ({label}): HTTP {response.status_code} {response.data}")
    results = mistapi.get_all(apisession, response)
    if isinstance(results, Mapping):
        results = results.get("results")
    if not isinstance(results, list) or not all(isinstance(r, Mapping) for r in results):
        raise TypeError(f"MistAPI returned an unexpected {label} response.")
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

    if args.token:
        # Token auth (from --token, MIST_APITOKEN, or .env): no email/password prompt.
        apisession = mistapi.APISession(apitoken=args.token, host=args.host)
    elif args.org_id:
        raise SystemExit(
            "--org-id needs an API token to run without prompts. Provide --token, set "
            "MIST_APITOKEN, or put MIST_APITOKEN in a .env file (current or script folder)."
        )
    else:
        apisession = mistapi.APISession()  # interactive login
    apisession.login()
    if not apisession.get_authentication_status():
        raise SystemExit("Mist authentication failed. Check the API token and host.")
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
    output_path = args.output or Path.cwd() / f"{args.type}_clients_last_{days}_days_{timestamp}.csv"
    export_clients(clients, output_path)
    print(f"Exported {len(clients)} clients to {output_path.resolve()}")


if __name__ == "__main__":
    main()
