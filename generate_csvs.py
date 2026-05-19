#!/usr/bin/env python3
"""
Faireez Dashboard — Daily CSV/JSON Data Generator
==================================================
Run this script once daily (e.g. via cron) to refresh all static data files
used by the Vercel-hosted static dashboard (static/index_static.html).

Usage:
    python generate_csvs.py

All output files are written to ./static/data/
"""

import csv
import json
import os
import sys
import datetime
from contextlib import contextmanager
from pathlib import Path
from decimal import Decimal

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "static" / "data"

_DB = dict(
    host="faireez-db.ceaaeaabvoqy.us-east-1.rds.amazonaws.com",
    port=5432,
    dbname="faireez",
    user="postgres",
    password=os.environ["DB_PASSWORD"],
)

_LOC_GUARD = 'l."IsTest" = false AND l."Status" = \'ACTIVE\''
_V_STATUS   = "v.\"Status\" IN ('FINISHED','COMPLETED','PENDING','CANCELLED')"
_USD_PRICE  = lambda col: (
    f'CASE WHEN l."Currency" = \'ILS\' THEN {col} / 3.65 '
    f'WHEN l."Currency" = \'GBP\' THEN {col} * 1.27 ELSE {col} END'
)
_NS_TYPE = """
    CASE
        WHEN req->>'frequency' IN (
            'once-a-week','twice-a-week','three-a-week','four-times-a-week',
            'every-day','once-in-2-week','once-a-month'
        ) THEN 'Subscription'
        ELSE 'One-time'
    END
"""


# ── DB helper ──────────────────────────────────────────────────────────────────

@contextmanager
def db():
    conn = psycopg2.connect(**_DB)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Serialisation helpers ──────────────────────────────────────────────────────

def _jsonify(obj):
    """JSON-safe default for Decimal / date types."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def save_json(filename: str, data):
    path = DATA_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, default=_jsonify, indent=2)
    print(f"  ✓ {filename}")


def save_csv(filename: str, rows: list):
    if not rows:
        print(f"  ⚠  {filename}: no rows — skipping")
        return
    path = DATA_DIR / filename
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (float(v) if isinstance(v, Decimal) else v)
                             for k, v in row.items()})
    print(f"  ✓ {filename} ({len(rows)} rows)")


# ── Funnel ─────────────────────────────────────────────────────────────────────

def generate_funnel():
    print("\nFunnel…")
    with db() as cur:

        # Residents
        cur.execute("""
            SELECT
                (SELECT COUNT(*) FROM "Apartments" a2
                 JOIN "Locations" l2 ON l2."Id" = a2."LocationId"
                 WHERE l2."IsTest" = false AND l2."Status" = 'ACTIVE') AS total_apartments,
                COALESCE(
                    (SELECT SUM(NULLIF("ApproximateNumberOfApartments", 0))
                     FROM "Locations"
                     WHERE "IsTest" = false AND "Status" = 'ACTIVE'),
                    0
                ) AS estimated_residents
        """)
        save_json("funnel_residents.json", dict(cur.fetchone()))

        # Summary KPIs
        cur.execute(f"""
            WITH base AS (
                SELECT
                    a."Id"   AS apt_id,
                    a."Status",
                    rr."ApartmentId" IS NOT NULL AS is_registered
                FROM "Apartments" a
                LEFT JOIN "Customers"    c   ON c."ApartmentId" = a."Id"
                LEFT JOIN "Accounts"     acc ON acc."Id" = c."AccountId"
                JOIN  "Locations"        l   ON l."Id" = a."LocationId"
                LEFT JOIN (SELECT DISTINCT "ApartmentId" FROM "RegistrationRequests") rr
                  ON rr."ApartmentId" = a."Id"
                WHERE {_LOC_GUARD}
            )
            SELECT
                COUNT(DISTINCT apt_id)                                                        AS leads,
                COUNT(DISTINCT apt_id) FILTER (WHERE is_registered)                          AS registered_leads,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))                        AS subscribers,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" = 'RECURRING_SUBSCRIPTION')   AS recurring,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" = 'ON_DEMAND_SUBSCRIPTION')   AS on_demand,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" NOT IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))                        AS non_subscribers,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" IN ('CHURN','FROZEN'))         AS churn,
                (SELECT COUNT(*) FROM "WaitingList"
                 WHERE "Step" = 'WAITING_LIST' AND "IsActive" = true)                        AS waitlist,
                (SELECT ROUND(SUM(
                    CASE WHEN l2."Currency" = 'ILS' THEN rb."Price" / 3.65
                         WHEN l2."Currency" = 'GBP' THEN rb."Price" * 1.27
                         ELSE rb."Price" END
                 )::numeric, 2)
                 FROM "RecurringBookings" rb
                 JOIN "Apartments" a2 ON a2."Id" = rb."ApartmentId"
                 JOIN "Locations"   l2 ON l2."Id" = a2."LocationId"
                 WHERE rb."Active" = true AND l2."IsTest" = false)                           AS mrr
            FROM base
        """)
        save_json("funnel_summary.json", dict(cur.fetchone()))

        # Monthly cohort timeseries
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', acc."CreatedAt"), 'YYYY-MM') AS cohort_month,
                COUNT(DISTINCT a."Id")                                   AS leads,
                COUNT(DISTINCT a."Id") FILTER (WHERE rr."ApartmentId" IS NOT NULL) AS registered_leads,
                COUNT(DISTINCT a."Id") FILTER (WHERE a."Status" IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))   AS subscribers
            FROM "Apartments" a
            LEFT JOIN "Customers"          c   ON c."ApartmentId" = a."Id"
            LEFT JOIN "Accounts"           acc ON acc."Id" = c."AccountId"
            JOIN  "Locations"              l   ON l."Id" = a."LocationId"
            LEFT JOIN "RegistrationRequests" rr ON rr."ApartmentId" = a."Id"
            WHERE {_LOC_GUARD} AND acc."CreatedAt" IS NOT NULL
            GROUP BY DATE_TRUNC('month', acc."CreatedAt")
            ORDER BY cohort_month
        """)
        save_csv("funnel_timeseries.csv", [dict(r) for r in cur.fetchall()])

        # Conversion source (latest subscription event per apartment)
        cur.execute(f"""
            WITH latest_sub AS (
                SELECT DISTINCT ON ("ApartmentId")
                    "ApartmentId", "NewStatus", "ChangedBy"
                FROM "ApartmentStatusHistory"
                WHERE "NewStatus" IN ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION')
                  AND "ChangedBy" IS NOT NULL AND "ChangedBy" != 0
                ORDER BY "ApartmentId", "CreatedAt" DESC
            )
            SELECT
                CASE WHEN self_cust."AccountId" IS NOT NULL THEN 'Self' ELSE 'Admin' END
                    AS converted_by,
                ls."NewStatus" AS subscription_type,
                COUNT(*) AS count
            FROM latest_sub ls
            JOIN  "Apartments"    a         ON a."Id" = ls."ApartmentId"
            LEFT JOIN "Customers" self_cust ON self_cust."ApartmentId" = ls."ApartmentId"
              AND self_cust."AccountId" = ls."ChangedBy"
            LEFT JOIN "Customers" c         ON c."ApartmentId" = a."Id"
            LEFT JOIN "Accounts"  acc       ON acc."Id" = c."AccountId"
            JOIN  "Locations"     l         ON l."Id" = a."LocationId"
            WHERE {_LOC_GUARD}
            GROUP BY converted_by, ls."NewStatus"
            ORDER BY converted_by, ls."NewStatus"
        """)
        save_csv("funnel_conversion_source.csv", [dict(r) for r in cur.fetchall()])

        # Conversion source (first subscription event per apartment)
        cur.execute(f"""
            WITH first_sub AS (
                SELECT DISTINCT ON ("ApartmentId")
                    "ApartmentId", "NewStatus", "ChangedBy"
                FROM "ApartmentStatusHistory"
                WHERE "NewStatus" IN ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION')
                  AND "ChangedBy" IS NOT NULL AND "ChangedBy" != 0
                ORDER BY "ApartmentId", "CreatedAt" ASC
            )
            SELECT
                CASE WHEN self_cust."AccountId" IS NOT NULL THEN 'Self' ELSE 'Admin' END
                    AS converted_by,
                fs."NewStatus" AS subscription_type,
                COUNT(*) AS count
            FROM first_sub fs
            JOIN  "Apartments"    a         ON a."Id" = fs."ApartmentId"
            LEFT JOIN "Customers" self_cust ON self_cust."ApartmentId" = fs."ApartmentId"
              AND self_cust."AccountId" = fs."ChangedBy"
            LEFT JOIN "Customers" c         ON c."ApartmentId" = a."Id"
            LEFT JOIN "Accounts"  acc       ON acc."Id" = c."AccountId"
            JOIN  "Locations"     l         ON l."Id" = a."LocationId"
            WHERE {_LOC_GUARD}
            GROUP BY converted_by, fs."NewStatus"
            ORDER BY converted_by, fs."NewStatus"
        """)
        save_csv("funnel_conversion_source_first.csv", [dict(r) for r in cur.fetchall()])

        # MRR histogram (per apartment, active recurring bookings)
        cur.execute("""
            WITH apt_mrr AS (
                SELECT "ApartmentId", SUM("Price") AS mrr
                FROM "RecurringBookings"
                WHERE "Active" = true
                GROUP BY "ApartmentId"
            )
            SELECT
                CASE
                    WHEN mrr < 50   THEN '0-50'
                    WHEN mrr < 100  THEN '50-100'
                    WHEN mrr < 150  THEN '100-150'
                    WHEN mrr < 200  THEN '150-200'
                    WHEN mrr < 300  THEN '200-300'
                    WHEN mrr < 400  THEN '300-400'
                    WHEN mrr < 500  THEN '400-500'
                    WHEN mrr < 750  THEN '500-750'
                    WHEN mrr < 1000 THEN '750-1000'
                    ELSE '1000+'
                END AS bucket,
                CASE
                    WHEN mrr < 50   THEN 0   WHEN mrr < 100  THEN 50
                    WHEN mrr < 150  THEN 100  WHEN mrr < 200  THEN 150
                    WHEN mrr < 300  THEN 200  WHEN mrr < 400  THEN 300
                    WHEN mrr < 500  THEN 400  WHEN mrr < 750  THEN 500
                    WHEN mrr < 1000 THEN 750  ELSE 1000
                END AS bucket_order,
                COUNT(*) AS count
            FROM apt_mrr
            GROUP BY bucket, bucket_order
            ORDER BY bucket_order
        """)
        save_csv("funnel_mrr_histogram.csv", [dict(r) for r in cur.fetchall()])

        # Adhoc spend histogram
        cur.execute(f"""
            WITH apt_spend AS (
                SELECT
                    v."ApartmentId",
                    SUM(v."FinalPrice") /
                        NULLIF(COUNT(DISTINCT DATE_TRUNC('month', v."Date")), 0) AS avg_monthly
                FROM "VisitsNew" v
                JOIN  "Apartments" a ON a."Id" = v."ApartmentId"
                JOIN  "Locations"  l ON l."Id" = a."LocationId"
                WHERE l."IsTest" = false AND l."Status" = 'ACTIVE'
                  AND v."Status" IN ('FINISHED','COMPLETED')
                  AND v."FinalPrice" > 0
                GROUP BY v."ApartmentId"
            )
            SELECT
                CASE
                    WHEN avg_monthly < 50   THEN '0-50'
                    WHEN avg_monthly < 100  THEN '50-100'
                    WHEN avg_monthly < 150  THEN '100-150'
                    WHEN avg_monthly < 200  THEN '150-200'
                    WHEN avg_monthly < 300  THEN '200-300'
                    WHEN avg_monthly < 400  THEN '300-400'
                    WHEN avg_monthly < 500  THEN '400-500'
                    WHEN avg_monthly < 750  THEN '500-750'
                    ELSE '750+'
                END AS bucket,
                CASE
                    WHEN avg_monthly < 50   THEN 0   WHEN avg_monthly < 100  THEN 50
                    WHEN avg_monthly < 150  THEN 100  WHEN avg_monthly < 200  THEN 150
                    WHEN avg_monthly < 300  THEN 200  WHEN avg_monthly < 400  THEN 300
                    WHEN avg_monthly < 500  THEN 400  WHEN avg_monthly < 750  THEN 500
                    ELSE 750
                END AS bucket_order,
                COUNT(*) AS count
            FROM apt_spend
            GROUP BY bucket, bucket_order
            ORDER BY bucket_order
        """)
        save_csv("funnel_adhoc_histogram.csv", [dict(r) for r in cur.fetchall()])

        # Cumulative active subscribers snapshot per month
        cur.execute("""
            WITH months AS (
                SELECT generate_series(
                    DATE_TRUNC('month', MIN("CreatedAt")),
                    DATE_TRUNC('month', NOW()),
                    INTERVAL '1 month'
                ) AS month_start
                FROM "ApartmentStatusHistory"
            ),
            latest_per_apt_month AS (
                SELECT DISTINCT ON (m.month_start, ash."ApartmentId")
                    m.month_start,
                    ash."ApartmentId",
                    ash."NewStatus"
                FROM months m
                JOIN "ApartmentStatusHistory" ash
                  ON ash."CreatedAt" < m.month_start + INTERVAL '1 month'
                WHERE ash."NewStatus" IS NOT NULL
                ORDER BY m.month_start, ash."ApartmentId", ash."CreatedAt" DESC
            )
            SELECT
                TO_CHAR(month_start, 'YYYY-MM')                                                AS sub_month,
                COUNT(*) FILTER (WHERE "NewStatus" IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))                          AS active_subscribers,
                COUNT(*) FILTER (WHERE "NewStatus" = 'RECURRING_SUBSCRIPTION')                 AS recurring,
                COUNT(*) FILTER (WHERE "NewStatus" = 'ON_DEMAND_SUBSCRIPTION')                 AS on_demand
            FROM latest_per_apt_month
            GROUP BY month_start
            ORDER BY month_start
        """)
        save_csv("funnel_cumulative_subscribers.csv", [dict(r) for r in cur.fetchall()])

        # Breakdown by project + neighbourhood
        cur.execute(f"""
            SELECT
                COALESCE(l."Project", 'Unknown') AS project,
                COALESCE(n."Project", 'Unknown') AS neighbourhood,
                COUNT(DISTINCT a."Id")           AS leads,
                COUNT(DISTINCT a."Id") FILTER (WHERE rr."ApartmentId" IS NOT NULL) AS registered_leads,
                COUNT(DISTINCT a."Id") FILTER (WHERE a."Status" IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))              AS subscribers,
                ROUND(
                    COUNT(DISTINCT a."Id") FILTER (WHERE a."Status" IN
                      ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))
                    * 100.0 / NULLIF(COUNT(DISTINCT a."Id"), 0), 1
                ) AS conversion_pct
            FROM "Apartments" a
            LEFT JOIN "Customers"          c   ON c."ApartmentId" = a."Id"
            LEFT JOIN "Accounts"           acc ON acc."Id" = c."AccountId"
            JOIN  "Locations"              l   ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods"      n   ON n."Id" = a."NeighborhoodId"
            LEFT JOIN "RegistrationRequests" rr ON rr."ApartmentId" = a."Id"
            WHERE {_LOC_GUARD}
            GROUP BY l."Project", n."Project"
            ORDER BY leads DESC
            LIMIT 100
        """)
        save_csv("funnel_breakdown.csv", [dict(r) for r in cur.fetchall()])


# ── Visits ─────────────────────────────────────────────────────────────────────

def generate_visits():
    print("\nVisits…")
    with db() as cur:

        # Summary KPIs
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED')) AS finished_completed,
                COUNT(*) FILTER (WHERE v."Status" = 'FINISHED')               AS finished,
                COUNT(*) FILTER (WHERE v."Status" = 'COMPLETED')              AS completed,
                COUNT(*) FILTER (WHERE v."Status" = 'PENDING')                AS pending,
                COUNT(*) FILTER (WHERE v."Status" = 'CANCELLED')              AS cancelled,
                ROUND(SUM(({_USD_PRICE('v."FinalPrice"')}))
                  FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED'))::numeric, 2) AS net_revenue
            FROM "VisitsNew" v
            JOIN  "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id" = a."LocationId"
            WHERE {_LOC_GUARD} AND {_V_STATUS}
        """)
        save_json("visits_summary.json", dict(cur.fetchone()))

        # Monthly timeseries
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COUNT(*) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED')) AS finished_completed,
                COUNT(*) FILTER (WHERE v."Status" = 'PENDING')                AS pending,
                COUNT(*) FILTER (WHERE v."Status" = 'CANCELLED')              AS cancelled,
                ROUND(
                    COUNT(*) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED')) * 100.0
                    / NULLIF(COUNT(*) FILTER (WHERE v."Status" IN
                      ('FINISHED','COMPLETED','CANCELLED')), 0), 1)           AS finished_pct,
                ROUND(AVG(v."NetDuration" * 60) FILTER (WHERE v."Status" IN
                  ('FINISHED','COMPLETED') AND v."NetDuration" > 0)::numeric, 0) AS avg_duration_mins
            FROM "VisitsNew" v
            JOIN  "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id" = a."LocationId"
            WHERE {_LOC_GUARD} AND {_V_STATUS}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """)
        save_csv("visits_timeseries.csv", [dict(r) for r in cur.fetchall()])

        # Chores summary
        cur.execute(f"""
            SELECT
                COUNT(vt."Id")                       AS chore_count,
                ROUND(SUM(t."Price")::numeric, 2)    AS total_amount
            FROM "VisitTasks" vt
            JOIN  "VisitsNew"  v ON v."Id"  = vt."VisitId"
            JOIN  "Apartments" a ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            JOIN  "Tasks"      t ON t."Id"  = vt."EntityId"
            WHERE vt."EntityType" = 'task'
              AND {_LOC_GUARD} AND v."Status" IN ('FINISHED','COMPLETED')
        """)
        save_json("visits_chores_summary.json", dict(cur.fetchone()))

        # Chores monthly timeseries
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                ROUND(COUNT(vt."Id")::numeric /
                  NULLIF(COUNT(DISTINCT v."Id"), 0), 2)             AS avg_tasks_per_visit,
                ROUND(AVG({_USD_PRICE('v."FinalPrice"')})::numeric, 2) AS avg_visit_price
            FROM "VisitsNew"  v
            JOIN  "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id" = a."LocationId"
            LEFT JOIN "VisitTasks" vt ON vt."VisitId" = v."Id" AND vt."EntityType" = 'task'
            WHERE {_LOC_GUARD} AND v."Status" IN ('FINISHED','COMPLETED')
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """)
        save_csv("visits_chores_timeseries.csv", [dict(r) for r in cur.fetchall()])

        # Last minute deals — by user type
        cur.execute(f"""
            WITH latest_lmd AS (
                SELECT DISTINCT ON ("EntityId")
                    "EntityId" AS visit_id,
                    "ApartmentStatus"
                FROM "UpsellPurchaseHistory"
                WHERE "Origin" = 'LAST_MINUTE_DEALS_CAROUSEL'
                ORDER BY "EntityId", "CreatedAt" DESC
            )
            SELECT
                CASE
                    WHEN COALESCE(lmd."ApartmentStatus", a."Status") = 'RECURRING_SUBSCRIPTION'
                        THEN 'Subscribers'
                    WHEN COALESCE(lmd."ApartmentStatus", a."Status") = 'ON_DEMAND_SUBSCRIPTION'
                        THEN 'On Demand'
                    ELSE 'Free / Other'
                END AS user_type,
                COUNT(DISTINCT lmd.visit_id) AS visit_count
            FROM latest_lmd lmd
            JOIN  "VisitsNew"  v ON v."Id"  = lmd.visit_id
            JOIN  "Apartments" a ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE {_LOC_GUARD} AND v."Status" IN ('FINISHED','COMPLETED')
            GROUP BY user_type
            ORDER BY visit_count DESC
        """)
        lmd_by_type = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            WITH latest_lmd AS (
                SELECT DISTINCT ON ("EntityId")
                    "EntityId" AS visit_id
                FROM "UpsellPurchaseHistory"
                WHERE "Origin" = 'LAST_MINUTE_DEALS_CAROUSEL'
                ORDER BY "EntityId", "CreatedAt" DESC
            )
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COUNT(DISTINCT lmd.visit_id)                       AS lmd_count
            FROM latest_lmd lmd
            JOIN  "VisitsNew"  v ON v."Id"  = lmd.visit_id
            JOIN  "Apartments" a ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE {_LOC_GUARD} AND v."Status" IN ('FINISHED','COMPLETED')
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """)
        save_json("visits_last_minute.json",
                  {"by_type": lmd_by_type,
                   "timeseries": [dict(r) for r in cur.fetchall()]})

        # Cancellations — by category
        cur.execute(f"""
            SELECT
                CASE
                    WHEN vc."CancelType" IN
                      ('APP_CANCELLED_OUT_POLICY','ADMIN_CANCELLED_OUT_POLICY')
                        THEN CASE WHEN COALESCE(v."FinalPrice",0) > 0
                             THEN 'OOP Charged' ELSE 'OOP Free' END
                    WHEN vc."CancelType" IN
                      ('APP_CANCELLED_IN_POLICY','ADMIN_CANCELLED_IN_POLICY')
                        THEN 'In Policy'
                    WHEN vc."CancelType" = 'APP_CANCELLED_IN_FREE_TRIAL'
                        THEN 'From Plan'
                    WHEN vc."CancelType" IN
                      ('ADMIN_FREE_CANCELLATION','APP_FREE_CANCELLATION')
                        THEN 'From Free'
                    ELSE 'Other'
                END AS cancel_category,
                CASE
                    WHEN lower(COALESCE(vc."Comments",'')) SIMILAR TO
                        '%%(duplicate|double.booking|rebook|dbl|dup |dup$|ooc|edit|removed|'
                        'churn|frozen|reschedul|cancel ahead|no response|ops |admin |bug |'
                        'scripted|outstanding balance)%%'
                    THEN 'Ops'
                    WHEN vc."CancelType" LIKE 'ADMIN_%%' THEN 'Admin'
                    ELSE 'Customer'
                END AS cancel_source,
                COUNT(*) AS count
            FROM "VisitCancellations" vc
            JOIN  "VisitsNew"  v ON v."Id"  = vc."VisitId"
            JOIN  "Apartments" a ON a."Id"  = vc."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE vc."CancelType" NOT IN ('APP_RESCHEDULE_NOW','APP_RESCHEDULE_LATER')
              AND {_LOC_GUARD}
            GROUP BY cancel_category, cancel_source
            ORDER BY count DESC
        """)
        by_cat = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COUNT(*) FILTER (WHERE vc."CancelType" IN
                  ('APP_CANCELLED_OUT_POLICY','ADMIN_CANCELLED_OUT_POLICY'))   AS out_of_policy,
                COUNT(*) FILTER (WHERE vc."CancelType" IN
                  ('APP_CANCELLED_IN_POLICY','ADMIN_CANCELLED_IN_POLICY'))     AS in_policy,
                COUNT(*) FILTER (WHERE vc."CancelType" = 'APP_CANCELLED_IN_FREE_TRIAL') AS from_plan,
                COUNT(*) FILTER (WHERE vc."CancelType" IN
                  ('ADMIN_FREE_CANCELLATION','APP_FREE_CANCELLATION'))         AS from_free
            FROM "VisitCancellations" vc
            JOIN  "VisitsNew"  v ON v."Id"  = vc."VisitId"
            JOIN  "Apartments" a ON a."Id"  = vc."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE vc."CancelType" NOT IN ('APP_RESCHEDULE_NOW','APP_RESCHEDULE_LATER')
              AND {_LOC_GUARD}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """)
        cancel_ts = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COUNT(*) FILTER (WHERE vc."CancelType" LIKE 'APP_%%')   AS app_cancelled,
                COUNT(*) FILTER (WHERE vc."CancelType" LIKE 'ADMIN_%%') AS admin_cancelled
            FROM "VisitCancellations" vc
            JOIN  "VisitsNew"  v ON v."Id"  = vc."VisitId"
            JOIN  "Apartments" a ON a."Id"  = vc."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE vc."CancelType" NOT IN ('APP_RESCHEDULE_NOW','APP_RESCHEDULE_LATER')
              AND {_LOC_GUARD}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """)
        save_json("visits_cancellations.json",
                  {"by_category": by_cat,
                   "timeseries": cancel_ts,
                   "by_source_ts": [dict(r) for r in cur.fetchall()]})

        # Duration histogram
        cur.execute(f"""
            WITH buckets AS (
                SELECT LEAST(FLOOR(v."NetDuration" * 60 / 30) * 30, 360) AS bucket_min
                FROM "VisitsNew" v
                JOIN  "Apartments" a ON a."Id" = v."ApartmentId"
                JOIN  "Locations"  l ON l."Id" = a."LocationId"
                WHERE {_LOC_GUARD}
                  AND v."Status" IN ('FINISHED','COMPLETED')
                  AND v."NetDuration" > 0
            )
            SELECT bucket_min::int AS bucket_min, COUNT(*) AS count
            FROM buckets
            GROUP BY bucket_min
            ORDER BY bucket_min
        """)
        def _dur_label(b):
            return "360+ min" if b >= 360 else f"{b}-{b+30} min"
        save_csv("visits_duration_histogram.csv",
                 [{"label": _dur_label(r["bucket_min"]),
                   "bucket_min": r["bucket_min"],
                   "count": r["count"]}
                  for r in cur.fetchall()])

        # Task distribution (top 30)
        cur.execute(f"""
            SELECT t."Title" AS task_name, COUNT(*) AS count
            FROM "VisitTasks" vt
            JOIN  "VisitsNew"  v ON v."Id"  = vt."VisitId"
            JOIN  "Apartments" a ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            JOIN  "Tasks"      t ON t."Id"  = vt."EntityId"
            WHERE vt."EntityType" = 'task'
              AND v."Status" IN ('FINISHED','COMPLETED')
              AND {_LOC_GUARD}
            GROUP BY t."Title"
            ORDER BY count DESC
            LIMIT 30
        """)
        save_csv("visits_task_distribution.csv", [dict(r) for r in cur.fetchall()])

        # Bookings by source (App vs Admin)
        cur.execute(f"""
            SELECT
                CASE
                    WHEN v."ChangedByAccount" IS NULL THEN 'App'
                    WHEN EXISTS (
                        SELECT 1 FROM "Customers" c
                        WHERE c."ApartmentId" = v."ApartmentId"
                          AND c."AccountId" = v."ChangedByAccount"
                    ) THEN 'App'
                    ELSE 'Admin'
                END AS booked_by,
                COUNT(*) AS count
            FROM "VisitsNew" v
            JOIN  "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id" = a."LocationId"
            WHERE {_LOC_GUARD} AND v."Status" IN ('FINISHED','COMPLETED')
            GROUP BY booked_by
        """)
        bbs_summary = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COUNT(*) FILTER (WHERE v."ChangedByAccount" IS NULL OR EXISTS (
                    SELECT 1 FROM "Customers" c
                    WHERE c."ApartmentId" = v."ApartmentId"
                      AND c."AccountId" = v."ChangedByAccount"
                )) AS app_booked,
                COUNT(*) FILTER (WHERE v."ChangedByAccount" IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM "Customers" c
                    WHERE c."ApartmentId" = v."ApartmentId"
                      AND c."AccountId" = v."ChangedByAccount"
                )) AS admin_booked
            FROM "VisitsNew" v
            JOIN  "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id" = a."LocationId"
            WHERE {_LOC_GUARD} AND v."Status" IN ('FINISHED','COMPLETED')
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """)
        save_json("visits_bookings_by_source.json",
                  {"summary": bbs_summary,
                   "timeseries": [dict(r) for r in cur.fetchall()]})


# ── Ratings ────────────────────────────────────────────────────────────────────

def generate_ratings():
    print("\nRatings…")
    with db() as cur:

        cur.execute(f"""
            SELECT
                COUNT(r."Id")                                    AS total_reviews,
                COUNT(r."CustomerRate")                          AS visit_rating_count,
                ROUND(AVG(r."CustomerRate")::numeric, 2)         AS avg_visit_rating,
                COUNT(r."FaireeRate")                            AS app_rating_count,
                ROUND(AVG(r."FaireeRate")::numeric, 2)           AS avg_app_rating
            FROM "Reviews" r
            JOIN  "VisitsNew"  v ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE {_LOC_GUARD}
        """)
        save_json("ratings_summary.json", dict(cur.fetchone()))

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS month,
                COUNT(r."Id")                                      AS review_count,
                ROUND(AVG(r."CustomerRate")::numeric, 2)           AS avg_visit_rating,
                ROUND(AVG(r."FaireeRate")::numeric, 2)             AS avg_app_rating
            FROM "Reviews" r
            JOIN  "VisitsNew"  v ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE {_LOC_GUARD}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY month
        """)
        save_csv("ratings_timeseries.csv", [dict(r) for r in cur.fetchall()])

        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE r."CustomerReview" IS NOT NULL
                  AND r."CustomerReview" != '' AND r."CustomerRate" = 5)   AS cust_positive,
                COUNT(*) FILTER (WHERE r."CustomerReview" IS NOT NULL
                  AND r."CustomerReview" != ''
                  AND r."CustomerRate" BETWEEN 3 AND 4)                    AS cust_neutral,
                COUNT(*) FILTER (WHERE r."CustomerReview" IS NOT NULL
                  AND r."CustomerReview" != '' AND r."CustomerRate" < 3)   AS cust_negative,
                COUNT(*) FILTER (WHERE r."CustomerReview" IS NOT NULL
                  AND r."CustomerReview" != ''
                  AND r."CustomerRate" IS NULL)                            AS cust_no_rating,
                COUNT(*) FILTER (WHERE r."FaireeReview" IS NOT NULL
                  AND r."FaireeReview" != '' AND r."FaireeRate" = 5)       AS fairee_positive,
                COUNT(*) FILTER (WHERE r."FaireeReview" IS NOT NULL
                  AND r."FaireeReview" != ''
                  AND r."FaireeRate" BETWEEN 3 AND 4)                      AS fairee_neutral,
                COUNT(*) FILTER (WHERE r."FaireeReview" IS NOT NULL
                  AND r."FaireeReview" != '' AND r."FaireeRate" < 3)       AS fairee_negative,
                COUNT(*) FILTER (WHERE r."FaireeReview" IS NOT NULL
                  AND r."FaireeReview" != ''
                  AND r."FaireeRate" IS NULL)                              AS fairee_no_rating
            FROM "Reviews" r
            JOIN  "VisitsNew"  v ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE {_LOC_GUARD}
        """)
        save_json("ratings_reviews.json", dict(cur.fetchone()))

        cur.execute(f"""
            SELECT ROUND(r."CustomerRate"::numeric) AS star,
                   COUNT(*) AS visit_count
            FROM "Reviews" r
            JOIN  "VisitsNew"  v ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE {_LOC_GUARD} AND r."CustomerRate" IS NOT NULL
            GROUP BY ROUND(r."CustomerRate"::numeric)
            ORDER BY star
        """)
        visit_dist = {r["star"]: r["visit_count"] for r in cur.fetchall()}

        cur.execute(f"""
            SELECT ROUND(r."FaireeRate"::numeric) AS star,
                   COUNT(*) AS app_count
            FROM "Reviews" r
            JOIN  "VisitsNew"  v ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l ON l."Id"  = a."LocationId"
            WHERE {_LOC_GUARD} AND r."FaireeRate" IS NOT NULL
            GROUP BY ROUND(r."FaireeRate"::numeric)
            ORDER BY star
        """)
        app_dist = {r["star"]: r["app_count"] for r in cur.fetchall()}

        stars = [1, 2, 3, 4, 5]
        save_json("ratings_distribution.json", {
            "visit": [{"star": s, "count": visit_dist.get(s, 0)} for s in stars],
            "app":   [{"star": s, "count": app_dist.get(s, 0)}   for s in stars],
        })


# ── No Slots ───────────────────────────────────────────────────────────────────

def generate_noslots():
    print("\nNo Slots…")
    with db() as cur:

        cur.execute(f"""
            WITH classified AS (
                SELECT
                    ns."ApartmentId",
                    DATE(ns."CreatedAt") AS event_date,
                    {_NS_TYPE} AS visit_type,
                    AVG(ns."TimeNeeded") FILTER (WHERE ns."TimeNeeded" > 0) AS avg_mins
                FROM "NoSlotEvents" ns
                LEFT JOIN LATERAL jsonb_array_elements(ns."Request") AS req ON true
                JOIN  "Apartments" a ON a."Id" = ns."ApartmentId"
                JOIN  "Locations"  l ON l."Id" = a."LocationId"
                WHERE {_LOC_GUARD}
                GROUP BY ns."ApartmentId", DATE(ns."CreatedAt"), visit_type
            ),
            with_success AS (
                SELECT c.*,
                    EXISTS (
                        SELECT 1 FROM "VisitsNew" v
                        WHERE v."ApartmentId" = c."ApartmentId"
                          AND DATE(v."CreatedAt") = c.event_date
                    ) AS found_slot
                FROM classified c
            )
            SELECT
                COUNT(*)                                             AS total_apt_days,
                COUNT(*) FILTER (WHERE visit_type = 'Subscription') AS subscription_apt_days,
                COUNT(*) FILTER (WHERE visit_type = 'One-time')     AS onetime_apt_days,
                ROUND(AVG(avg_mins) FILTER (WHERE visit_type = 'Subscription'
                  AND avg_mins IS NOT NULL)::numeric, 0)            AS avg_time_sub_mins,
                ROUND(AVG(avg_mins) FILTER (WHERE visit_type = 'One-time'
                  AND avg_mins IS NOT NULL)::numeric, 0)            AS avg_time_onetime_mins,
                SUM(found_slot::int)                                 AS found_slot_count,
                ROUND(SUM(found_slot::int) * 100.0 / NULLIF(COUNT(*), 0), 1) AS success_rate_pct
            FROM with_success
        """)
        save_json("noslots_summary.json", dict(cur.fetchone()))

        cur.execute(f"""
            WITH classified AS (
                SELECT
                    ns."ApartmentId",
                    DATE(ns."CreatedAt") AS event_date,
                    {_NS_TYPE} AS visit_type,
                    AVG(ns."TimeNeeded") FILTER (WHERE ns."TimeNeeded" > 0) AS avg_mins
                FROM "NoSlotEvents" ns
                LEFT JOIN LATERAL jsonb_array_elements(ns."Request") AS req ON true
                JOIN  "Apartments" a ON a."Id" = ns."ApartmentId"
                JOIN  "Locations"  l ON l."Id" = a."LocationId"
                WHERE {_LOC_GUARD}
                GROUP BY ns."ApartmentId", DATE(ns."CreatedAt"), visit_type
            ),
            with_success AS (
                SELECT c.*,
                    EXISTS (
                        SELECT 1 FROM "VisitsNew" v
                        WHERE v."ApartmentId" = c."ApartmentId"
                          AND DATE(v."CreatedAt") = c.event_date
                    ) AS found_slot
                FROM classified c
            )
            SELECT
                TO_CHAR(DATE_TRUNC('month', event_date), 'YYYY-MM')  AS month,
                COUNT(*) FILTER (WHERE visit_type = 'Subscription')   AS subscription_apt_days,
                COUNT(*) FILTER (WHERE visit_type = 'One-time')       AS onetime_apt_days,
                ROUND(AVG(avg_mins) FILTER (WHERE visit_type = 'Subscription'
                  AND avg_mins IS NOT NULL)::numeric, 0)              AS avg_time_sub_mins,
                ROUND(AVG(avg_mins) FILTER (WHERE visit_type = 'One-time'
                  AND avg_mins IS NOT NULL)::numeric, 0)              AS avg_time_onetime_mins,
                ROUND(SUM(found_slot::int) * 100.0 / NULLIF(COUNT(*), 0), 1) AS success_rate_pct
            FROM with_success
            GROUP BY DATE_TRUNC('month', event_date)
            ORDER BY month
        """)
        save_csv("noslots_timeseries.csv", [dict(r) for r in cur.fetchall()])

        cur.execute(f"""
            SELECT
                COUNT(*) AS total_incidents,
                COUNT(*) FILTER (WHERE {_NS_TYPE} = 'Subscription') AS sub_incidents,
                COUNT(*) FILTER (WHERE {_NS_TYPE} = 'One-time')     AS onetime_incidents
            FROM "NoSlotEvents" ns
            LEFT JOIN LATERAL jsonb_array_elements(ns."Request") AS req ON true
            JOIN  "Apartments" a ON a."Id" = ns."ApartmentId"
            JOIN  "Locations"  l ON l."Id" = a."LocationId"
            WHERE {_LOC_GUARD}
        """)
        ns_summary = dict(cur.fetchone())

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', ns."CreatedAt"), 'YYYY-MM') AS month,
                COUNT(*) AS total_incidents,
                COUNT(*) FILTER (WHERE {_NS_TYPE} = 'Subscription') AS sub_incidents,
                COUNT(*) FILTER (WHERE {_NS_TYPE} = 'One-time')     AS onetime_incidents
            FROM "NoSlotEvents" ns
            LEFT JOIN LATERAL jsonb_array_elements(ns."Request") AS req ON true
            JOIN  "Apartments" a ON a."Id" = ns."ApartmentId"
            JOIN  "Locations"  l ON l."Id" = a."LocationId"
            WHERE {_LOC_GUARD}
            GROUP BY DATE_TRUNC('month', ns."CreatedAt")
            ORDER BY month
        """)
        save_json("noslots_incidents.json",
                  {"summary": ns_summary,
                   "timeseries": [dict(r) for r in cur.fetchall()]})


# ── Raw Dimensional Data ───────────────────────────────────────────────────────

def generate_filters():
    """Generate filters.json with list of projects and neighbourhoods."""
    with db() as cur:
        cur.execute("""
            SELECT DISTINCT "Project" FROM "Locations"
            WHERE "Project" IS NOT NULL AND "IsTest" = false AND "Status" = 'ACTIVE'
            ORDER BY 1
        """)
        projects = [r["Project"] for r in cur.fetchall()]

        cur.execute('SELECT DISTINCT "Project" FROM "Neighborhoods" WHERE "Project" IS NOT NULL ORDER BY 1')
        neighbourhoods = [r["Project"] for r in cur.fetchall()]

    save_json("filters.json", {"projects": projects, "neighbourhoods": neighbourhoods})


def generate_funnel_raw():
    """Generate funnel_apts.csv — one row per apartment with all dimensions."""
    with db() as cur:
        cur.execute("""
            SELECT
                a."Id" AS apt_id,
                COALESCE(l."Project", 'Unknown') AS project,
                COALESCE(n."Project", 'Unknown') AS neighbourhood,
                TO_CHAR(DATE_TRUNC('month', acc."CreatedAt"), 'YYYY-MM') AS cohort_month,
                a."Status" AS apt_status,
                CASE WHEN rr."ApartmentId" IS NOT NULL THEN 'true' ELSE 'false' END AS is_registered,
                CASE WHEN a."Status" IN ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION') THEN 'true' ELSE 'false' END AS is_subscriber,
                CASE WHEN a."Status" = 'RECURRING_SUBSCRIPTION' THEN 'true' ELSE 'false' END AS is_recurring,
                CASE WHEN a."Status" = 'ON_DEMAND_SUBSCRIPTION' THEN 'true' ELSE 'false' END AS is_ondemand
            FROM "Apartments" a
            LEFT JOIN "Customers" c ON c."ApartmentId" = a."Id"
            LEFT JOIN "Accounts" acc ON acc."Id" = c."AccountId"
            JOIN "Locations" l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            LEFT JOIN (SELECT DISTINCT "ApartmentId" FROM "RegistrationRequests") rr ON rr."ApartmentId" = a."Id"
            WHERE l."IsTest" = false AND l."Status" = 'ACTIVE'
            ORDER BY cohort_month, project
        """)
        save_csv("funnel_apts.csv", [dict(r) for r in cur.fetchall()])


def generate_visits_raw():
    """Generate visits_monthly_raw.csv and visits_chores_monthly_raw.csv."""
    with db() as cur:
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COALESCE(l."Project", 'Unknown') AS project,
                COALESCE(n."Project", 'Unknown') AS neighbourhood,
                a."Status" AS apt_status,
                COUNT(*) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED')) AS finished_completed,
                COUNT(*) FILTER (WHERE v."Status" = 'FINISHED') AS finished,
                COUNT(*) FILTER (WHERE v."Status" = 'COMPLETED') AS completed,
                COUNT(*) FILTER (WHERE v."Status" = 'PENDING') AS pending,
                COUNT(*) FILTER (WHERE v."Status" = 'CANCELLED') AS cancelled,
                ROUND(SUM(CASE WHEN l."Currency" = 'ILS' THEN v."FinalPrice" / 3.65
                               WHEN l."Currency" = 'GBP' THEN v."FinalPrice" * 1.27
                               ELSE v."FinalPrice" END)
                      FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED'))::numeric, 2) AS net_revenue,
                ROUND(AVG(v."NetDuration" * 60) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED') AND v."NetDuration" > 0)::numeric, 0) AS avg_duration_mins
            FROM "VisitsNew" v
            JOIN "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN "Locations" l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE l."IsTest" = false AND l."Status" = 'ACTIVE'
              AND v."Status" IN ('FINISHED','COMPLETED','PENDING','CANCELLED')
              AND v."Date" IS NOT NULL
            GROUP BY DATE_TRUNC('month', v."Date"), l."Project", n."Project", a."Status"
            ORDER BY visit_month, project
        """)
        save_csv("visits_monthly_raw.csv", [dict(r) for r in cur.fetchall()])

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COALESCE(l."Project", 'Unknown') AS project,
                COALESCE(n."Project", 'Unknown') AS neighbourhood,
                a."Status" AS apt_status,
                ROUND(COUNT(vt."Id")::numeric / NULLIF(COUNT(DISTINCT v."Id"), 0), 2) AS avg_tasks_per_visit,
                ROUND(AVG(CASE WHEN l."Currency" = 'ILS' THEN v."FinalPrice" / 3.65
                               WHEN l."Currency" = 'GBP' THEN v."FinalPrice" * 1.27
                               ELSE v."FinalPrice" END)::numeric, 2) AS avg_visit_price,
                COUNT(vt."Id") AS chore_count,
                ROUND(SUM(t."Price")::numeric, 2) AS total_amount
            FROM "VisitsNew" v
            JOIN "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN "Locations" l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            LEFT JOIN "VisitTasks" vt ON vt."VisitId" = v."Id" AND vt."EntityType" = 'task'
            LEFT JOIN "Tasks" t ON t."Id" = vt."EntityId"
            WHERE l."IsTest" = false AND l."Status" = 'ACTIVE'
              AND v."Status" IN ('FINISHED','COMPLETED','PENDING','CANCELLED')
              AND v."Date" IS NOT NULL
            GROUP BY DATE_TRUNC('month', v."Date"), l."Project", n."Project", a."Status"
            ORDER BY visit_month, project
        """)
        save_csv("visits_chores_monthly_raw.csv", [dict(r) for r in cur.fetchall()])


def generate_ratings_raw():
    """Generate ratings_monthly_raw.csv."""
    with db() as cur:
        cur.execute("""
            SELECT
                TO_CHAR(DATE_TRUNC('month', rv."CreatedAt"), 'YYYY-MM') AS review_month,
                COALESCE(l."Project", 'Unknown') AS project,
                COALESCE(n."Project", 'Unknown') AS neighbourhood,
                a."Status" AS apt_status,
                COUNT(*) AS count,
                ROUND(AVG(rv."CustomerRate")::numeric, 2) AS avg_rating,
                COUNT(*) FILTER (WHERE rv."CustomerReview" IS NOT NULL AND rv."CustomerReview" != '') AS reviews
            FROM "Reviews" rv
            JOIN "VisitsNew" v ON v."Id" = rv."VisitId"
            JOIN "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN "Locations" l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE l."IsTest" = false AND l."Status" = 'ACTIVE'
              AND rv."CreatedAt" IS NOT NULL
            GROUP BY DATE_TRUNC('month', rv."CreatedAt"), l."Project", n."Project", a."Status"
            ORDER BY review_month, project
        """)
        save_csv("ratings_monthly_raw.csv", [dict(r) for r in cur.fetchall()])


def generate_noslots_raw():
    """Generate noslots_monthly_raw.csv."""
    with db() as cur:
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', ns."CreatedAt"), 'YYYY-MM') AS ns_month,
                COALESCE(l."Project", 'Unknown') AS project,
                COALESCE(n."Project", 'Unknown') AS neighbourhood,
                a."Status" AS apt_status,
                CASE
                    WHEN req->>'frequency' IN (
                        'once-a-week','twice-a-week','three-a-week','four-times-a-week',
                        'every-day','once-in-2-week','once-a-month'
                    ) THEN 'Subscription'
                    ELSE 'One-time'
                END AS visit_type,
                COUNT(DISTINCT ns."Id") AS count
            FROM "NoSlotEvents" ns
            LEFT JOIN LATERAL jsonb_array_elements(ns."Request") AS req ON true
            JOIN "Apartments" a ON a."Id" = ns."ApartmentId"
            JOIN "Locations" l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE l."IsTest" = false AND l."Status" = 'ACTIVE'
              AND ns."CreatedAt" IS NOT NULL
            GROUP BY DATE_TRUNC('month', ns."CreatedAt"), l."Project", n."Project", a."Status", visit_type
            ORDER BY ns_month, project
        """)
        save_csv("noslots_monthly_raw.csv", [dict(r) for r in cur.fetchall()])


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Faireez CSV Generator — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        generate_funnel()
        generate_visits()
        generate_ratings()
        generate_noslots()

        print("\nRaw dimensional data…")
        generate_filters()
        generate_funnel_raw()
        generate_visits_raw()
        generate_ratings_raw()
        generate_noslots_raw()
    except Exception as exc:
        print(f"\n✗ Error: {exc}", file=sys.stderr)
        raise

    # Write a timestamp file so the dashboard can show data freshness
    (DATA_DIR / "last_updated.json").write_text(
        json.dumps({"updated_at": datetime.datetime.utcnow().isoformat() + "Z"})
    )
    print(f"\n✓ All done — {datetime.datetime.now():%H:%M:%S}")
    print(f"  Files written to: {DATA_DIR}")
