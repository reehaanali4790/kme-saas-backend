"""Import a customer dump into tenant schema on shared SaaS platform."""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--dump", required=True, help="Path to pg_dump SQL file")
    args = parser.parse_args()
    print(f"Import {args.dump} into tenant_{args.slug} — run pg_restore/psql manually with schema mapping.")
    print("See docs/customer-migration-runbook.md")


if __name__ == "__main__":
    main()
