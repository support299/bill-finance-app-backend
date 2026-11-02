#!/usr/bin/env python3
"""Export ONE GoHighLevel location's data from a restored copy of the old (Lovable/Supabase) database to JSON.

Reads only; secrets are never exported (no Plaid access tokens, no sync cursors, no GHL OAuth tokens).

    pg_restore -n public --no-owner --no-acl -d lovable_scratch <dump>      # use the pg_restore matching the dump version
    python tools/export_lovable_location.py --db lovable_scratch --location zfdxaljeKDUix3jQL4Vd \
        --out lovable_proj_data/imax_digital_export.json

Then on the target:  python manage.py import_lovable lovable_proj_data/imax_digital_export.json --location <id> [--dry-run]
"""

import argparse
import json
import subprocess
import sys

# Column lists are explicit so a secret column can never leak into the file.
TABLES = {
    "ledgers": "id,label,contact_name,contact_email,contact_phone,notes,account_class,ghl_contact_id,created_at",
    "transactions": (
        "id,plaid_item_id,plaid_account_id,plaid_transaction_id,merchant_name,original_description,description,amount,currency,"
        "transaction_date,authorized_date,pending,payment_channel,transaction_type,merchant_entity_id,merchant_logo_url,website,"
        "city,region,postal_code,country,plaid_personal_finance_primary_category,plaid_personal_finance_detailed_category,"
        "plaid_category_confidence,user_category_id,category_source,categorization_confidence,transaction_direction,is_income,"
        "is_expense,is_transfer,is_recurring,is_hidden,is_demo,created_at,removed_at,ledger_id,source,status,due_date,"
        "accounting_status,gaap_type,external_ref"
    ),
    "bank_accounts": (
        "id,plaid_item_id,plaid_account_id,institution_id,account_name,official_name,mask,account_type,account_subtype,"
        "current_balance,available_balance,iso_currency_code,last_balance_update"
    ),
    "plaid_items": "id,item_id,institution_id,institution_name,environment,consented_at,last_sync_at,initial_sync_done",
    "ghl_invoices": (
        "id,location_id,ghl_invoice_id,ghl_contact_id,ledger_id,invoice_number,issue_date,due_date,currency,subtotal,discount,tax,"
        "total,amount_paid,amount_refunded,balance,ghl_status,status,needs_review,review_reason,raw,created_at"
    ),
    "ghl_invoice_payments": "id,invoice_id,ghl_transaction_id,amount,amount_refunded,fee,status,paid_at,reference,provider,raw",
    "ghl_invoice_events": (
        "id,invoice_id,ghl_invoice_id,source,event_type,previous_status,new_status,previous_balance,new_balance,payload,created_at"
    ),
}


def q(db: str, sql: str):
    out = subprocess.run(["psql", "-d", db, "-At", "-c", sql], check=True, capture_output=True, text=True).stdout.strip()
    return json.loads(out) if out else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="restored scratch database name")
    ap.add_argument("--location", required=True, help="GHL location id")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    loc = q(
        a.db,
        f"select row_to_json(l) from (select location_id, user_id, label, active from public.ghl_locations where location_id = '{a.location}') l",
    )
    if not loc:
        sys.exit(f"Location {a.location} not found in {a.db}")
    uid = loc["user_id"]
    data = {"format": 1, "location": {"location_id": loc["location_id"], "label": loc["label"], "active": loc["active"]}}
    for table, cols in TABLES.items():
        data[table] = q(
            a.db,
            f"select coalesce(json_agg(row_to_json(t)), '[]'::json) from (select {cols} from public.{table} where user_id = '{uid}') t",
        )
    # category names for the ids the transactions refer to, so they can be re-linked by name in the new database
    data["categories"] = q(
        a.db,
        f"select coalesce(json_agg(row_to_json(c)), '[]'::json) from (select id, name, type, is_system from public.categories "
        f"where id in (select user_category_id from public.transactions where user_id = '{uid}' and user_category_id is not null)) c",
    )
    with open(a.out, "w") as fh:
        json.dump(data, fh)
    print("Exported:", {k: len(v) for k, v in data.items() if isinstance(v, list)})


if __name__ == "__main__":
    main()
