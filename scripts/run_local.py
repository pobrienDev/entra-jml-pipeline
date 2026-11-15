#!/usr/bin/env python3
"""Run the joiner on a developer machine against the lab tenant.

    python scripts/run_local.py sample-data/hr_export_day1.csv --domain <lab-domain>
    python scripts/run_local.py sample-data/hr_export_day1.csv --domain <lab-domain> --apply

Without --apply this is a dry run: it reads the directory and reports what it
would do, and changes nothing. It uses the same code the Function app will;
the only difference is the credential, which here is your Azure CLI sign-in
(`az login`) instead of the managed identity.

--domain is the UPN domain new accounts get. It also guards against the wrong
tenant: the run stops unless the domain is a verified domain of the tenant
the Azure CLI is signed in to.
"""

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shared.auth import pipeline_credential  # noqa: E402
from shared.config import build_config  # noqa: E402
from shared.graph_api import GraphClient, GraphError  # noqa: E402
from shared.hr_feed import FeedError, parse_hr_export  # noqa: E402
from shared.onboard import FAILED, resolve_groups, run_joiners  # noqa: E402
from shared.rules import ProvisionError  # noqa: E402


def check_tenant(client, domain):
    """Stop unless `domain` belongs to the signed-in tenant; return its name."""
    tenant = client.get_tenant()
    verified = {str(d.get("name", "")).lower() for d in tenant.get("verifiedDomains") or []}
    if domain.lower() not in verified:
        raise ProvisionError(
            f"{domain} is not a verified domain of the tenant the Azure CLI is signed in to "
            f"({tenant.get('displayName', 'unknown')}) — wrong tenant, or wrong --domain"
        )
    return tenant.get("displayName", "unknown")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("csv", type=Path, help="HR export to process")
    parser.add_argument("--domain", required=True, help="UPN domain of the lab tenant")
    parser.add_argument("--apply", action="store_true",
                        help="make the changes (default is a dry run that changes nothing)")
    parser.add_argument("--verbose", action="store_true", help="show UPN collision handling")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="  %(message)s")
    dry_run = not args.apply

    try:
        config = build_config(args.domain)
        employees, errors = parse_hr_export(args.csv.read_text(encoding="utf-8"), config)
        client = GraphClient(pipeline_credential())
        tenant = check_tenant(client, args.domain)
        print(f"Tenant: {tenant}")
        print(f"Mode:   {'DRY RUN, nothing will change' if dry_run else 'APPLY'}")
        print(f"File:   {args.csv} ({len(employees)} valid row(s), {len(errors)} rejected)")
        for error in errors:
            print(f"  rejected: {error}")

        group_ids, missing = resolve_groups(client, config)
        if missing and not dry_run:
            raise ProvisionError(
                "these groups in the department map don't exist in the tenant: "
                f"{', '.join(missing)} — run terraform apply in infra/ first"
            )
        if missing:
            print(f"  note: {len(missing)} group(s) don't exist yet: {', '.join(missing)}")

        actions = run_joiners(client, employees, config, group_ids, dry_run)
    except (OSError, FeedError, GraphError, ProvisionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print()
    for a in actions:
        detail = f"  ({a['detail']})" if a["detail"] else ""
        print(f"  {a['employee_id']:<8} {a['action']:<13} {a['result']:<8} {a['target']}{detail}")
    counts = Counter(a["result"] for a in actions)
    print()
    print("Summary: " + ", ".join(f"{counts[r]} {r}" for r in ("done", "would", "skipped", "failed")))
    return 1 if counts[FAILED] or errors else 0


if __name__ == "__main__":
    sys.exit(main())
