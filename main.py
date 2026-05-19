#!/usr/bin/env python3
"""Faireez Health & Business KPIs — FastAPI backend."""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

app = FastAPI(title="Faireez KPI Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_DB = dict(
    host="faireez-db.ceaaeaabvoqy.us-east-1.rds.amazonaws.com",
    port=5432,
    dbname="faireez",
    user="postgres",
    password="D6RK%RZbNk*4!JV2rU",
)
PH_KEY = os.environ.get("POSTHOG_API_KEY", "")
PH_PROJECT = "176966"
PH_HOST = "https://us.posthog.com"

# Applied to every query — exclude test buildings, keep only active ones
_LOC_GUARD = 'l."IsTest" = false AND l."Status" = \'ACTIVE\''

# ILS and GBP → USD conversion (fixed exchange rates)
_USD_PRICE = lambda col: f'CASE WHEN l."Currency" = \'ILS\' THEN {col} / 3.65 WHEN l."Currency" = \'GBP\' THEN {col} * 1.27 ELSE {col} END'


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


def _extra(date_from=None, date_to=None, project=None, neighbourhood=None, sub_type=None):
    """Return (AND-prefixed SQL fragment, params list) for optional user filters."""
    clauses, params = [], []
    if date_from:
        clauses.append('acc."CreatedAt" >= %s')
        params.append(date_from)
    if date_to:
        clauses.append('acc."CreatedAt" <= %s')
        params.append(date_to)
    if project and project != "all":
        clauses.append('l."Project" = %s')
        params.append(project)
    if neighbourhood and neighbourhood != "all":
        clauses.append('n."Project" = %s')
        params.append(neighbourhood)
    if sub_type == 'recurring':
        clauses.append('a."Status" = \'RECURRING_SUBSCRIPTION\'')
    elif sub_type == 'ondemand':
        clauses.append('a."Status" = \'ON_DEMAND_SUBSCRIPTION\'')
    elif sub_type == 'non_sub':
        clauses.append('a."Status" NOT IN (\'RECURRING_SUBSCRIPTION\',\'ON_DEMAND_SUBSCRIPTION\')')
    sql = ("AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params


# ── Filter options ─────────────────────────────────────────────────────────────

@app.get("/api/filters/projects")
def get_projects():
    with db() as cur:
        cur.execute("""
            SELECT DISTINCT "Project" FROM "Locations"
            WHERE "Project" IS NOT NULL AND "IsTest" = false AND "Status" = 'ACTIVE'
            ORDER BY 1
        """)
        return [r["Project"] for r in cur.fetchall()]


@app.get("/api/filters/neighbourhoods")
def get_neighbourhoods():
    with db() as cur:
        cur.execute('SELECT DISTINCT "Project" FROM "Neighborhoods" WHERE "Project" IS NOT NULL ORDER BY 1')
        return [r["Project"] for r in cur.fetchall()]


# ── Funnel summary KPIs ────────────────────────────────────────────────────────

@app.get("/api/funnel/summary")
def funnel_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _extra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            WITH base AS (
                SELECT
                    a."Id"                                                          AS apt_id,
                    a."Status",
                    rr."ApartmentId" IS NOT NULL                                   AS is_registered
                FROM "Apartments" a
                LEFT JOIN "Customers"    c   ON c."ApartmentId" = a."Id"
                LEFT JOIN "Accounts"     acc ON acc."Id" = c."AccountId"
                JOIN  "Locations"        l   ON l."Id" = a."LocationId"
                LEFT JOIN "Neighborhoods" n  ON n."Id" = a."NeighborhoodId"
                LEFT JOIN (SELECT DISTINCT "ApartmentId" FROM "RegistrationRequests") rr
                  ON rr."ApartmentId" = a."Id"
                WHERE {_LOC_GUARD} {extra}
            )
            SELECT
                COUNT(DISTINCT apt_id)                                                               AS leads,
                COUNT(DISTINCT apt_id) FILTER (WHERE is_registered)                                 AS registered_leads,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))                               AS subscribers,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" = 'RECURRING_SUBSCRIPTION')           AS recurring,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" = 'ON_DEMAND_SUBSCRIPTION')           AS on_demand,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" NOT IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))                               AS non_subscribers,
                COUNT(DISTINCT apt_id) FILTER (WHERE "Status" IN ('CHURN','FROZEN'))                AS churn,
                (SELECT COUNT(*) FROM "WaitingList"
                 WHERE "Step" = 'WAITING_LIST' AND "IsActive" = true)                               AS waitlist,
                (SELECT ROUND(SUM(
                    CASE WHEN l2."Currency" = 'ILS' THEN rb."Price" / 3.65
                         WHEN l2."Currency" = 'GBP' THEN rb."Price" * 1.27
                         ELSE rb."Price" END
                 )::numeric, 2)
                 FROM "RecurringBookings" rb
                 JOIN "Apartments" a2 ON a2."Id" = rb."ApartmentId"
                 JOIN "Locations"   l2 ON l2."Id" = a2."LocationId"
                 WHERE rb."Active" = true AND l2."IsTest" = false)                                 AS mrr
            FROM base
        """, params)
        return dict(cur.fetchone())


# ── Funnel time-series (by cohort month) ──────────────────────────────────────

@app.get("/api/funnel/timeseries")
def funnel_timeseries(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _extra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', acc."CreatedAt"), 'YYYY-MM')            AS cohort_month,
                COUNT(DISTINCT a."Id")                                              AS leads,
                COUNT(DISTINCT a."Id") FILTER (WHERE rr."ApartmentId" IS NOT NULL) AS registered_leads,
                COUNT(DISTINCT a."Id") FILTER (WHERE a."Status" IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))              AS subscribers
            FROM "Apartments" a
            LEFT JOIN "Customers"          c   ON c."ApartmentId" = a."Id"
            LEFT JOIN "Accounts"           acc ON acc."Id" = c."AccountId"
            JOIN  "Locations"              l   ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods"      n   ON n."Id" = a."NeighborhoodId"
            LEFT JOIN "RegistrationRequests" rr ON rr."ApartmentId" = a."Id"
            WHERE {_LOC_GUARD} AND acc."CreatedAt" IS NOT NULL {extra}
            GROUP BY DATE_TRUNC('month', acc."CreatedAt")
            ORDER BY cohort_month
        """, params)
        return [dict(r) for r in cur.fetchall()]


# ── Breakdown by project / neighbourhood ──────────────────────────────────────

@app.get("/api/funnel/breakdown")
def funnel_breakdown(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _extra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            SELECT
                COALESCE(l."Project", 'Unknown')  AS project,
                COALESCE(n."Project", 'Unknown')  AS neighbourhood,
                COUNT(DISTINCT a."Id")            AS leads,
                COUNT(DISTINCT a."Id") FILTER (WHERE rr."ApartmentId" IS NOT NULL)   AS registered_leads,
                COUNT(DISTINCT a."Id") FILTER (WHERE a."Status" IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))                AS subscribers,
                ROUND(
                    COUNT(DISTINCT a."Id") FILTER (WHERE a."Status" IN
                        ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))
                    * 100.0 / NULLIF(COUNT(DISTINCT a."Id"), 0), 1
                )                                                                     AS conversion_pct
            FROM "Apartments" a
            LEFT JOIN "Customers"          c   ON c."ApartmentId" = a."Id"
            LEFT JOIN "Accounts"           acc ON acc."Id" = c."AccountId"
            JOIN  "Locations"              l   ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods"      n   ON n."Id" = a."NeighborhoodId"
            LEFT JOIN "RegistrationRequests" rr ON rr."ApartmentId" = a."Id"
            WHERE {_LOC_GUARD} {extra}
            GROUP BY l."Project", n."Project"
            ORDER BY leads DESC
            LIMIT 100
        """, params)
        return [dict(r) for r in cur.fetchall()]


# ── Conversion source: Self vs Admin ──────────────────────────────────────────

@app.get("/api/funnel/conversion-source")
def conversion_source(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _extra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            WITH latest_sub AS (
                SELECT DISTINCT ON ("ApartmentId")
                    "ApartmentId",
                    "NewStatus",
                    "ChangedBy"
                FROM "ApartmentStatusHistory"
                WHERE "NewStatus" IN ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION')
                  AND "ChangedBy" IS NOT NULL AND "ChangedBy" != 0
                ORDER BY "ApartmentId", "CreatedAt" DESC
            )
            SELECT
                CASE
                    WHEN self_cust."AccountId" IS NOT NULL THEN 'Self'
                    ELSE 'Admin'
                END                     AS converted_by,
                ls."NewStatus"          AS subscription_type,
                COUNT(*)                AS count
            FROM latest_sub ls
            JOIN  "Apartments"    a         ON a."Id"            = ls."ApartmentId"
            LEFT JOIN "Customers" self_cust ON self_cust."ApartmentId" = ls."ApartmentId"
              AND self_cust."AccountId" = ls."ChangedBy"
            LEFT JOIN "Customers" c         ON c."ApartmentId"  = a."Id"
            LEFT JOIN "Accounts"  acc       ON acc."Id"          = c."AccountId"
            JOIN  "Locations"     l         ON l."Id"            = a."LocationId"
            LEFT JOIN "Neighborhoods" n     ON n."Id"            = a."NeighborhoodId"
            WHERE {_LOC_GUARD} {extra}
            GROUP BY converted_by, ls."NewStatus"
            ORDER BY converted_by, ls."NewStatus"
        """, params)
        return [dict(r) for r in cur.fetchall()]


# ── MRR by apartment histogram ────────────────────────────────────────────────

@app.get("/api/funnel/mrr-histogram")
def mrr_histogram():
    with db() as cur:
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
                    WHEN mrr < 50   THEN 0
                    WHEN mrr < 100  THEN 50
                    WHEN mrr < 150  THEN 100
                    WHEN mrr < 200  THEN 150
                    WHEN mrr < 300  THEN 200
                    WHEN mrr < 400  THEN 300
                    WHEN mrr < 500  THEN 400
                    WHEN mrr < 750  THEN 500
                    WHEN mrr < 1000 THEN 750
                    ELSE 1000
                END AS bucket_order,
                COUNT(*) AS count
            FROM apt_mrr
            GROUP BY bucket, bucket_order
            ORDER BY bucket_order
        """)
        return [dict(r) for r in cur.fetchall()]


# ── Avg adhoc monthly spend by apartment histogram ────────────────────────────

@app.get("/api/funnel/adhoc-spend-histogram")
def adhoc_spend_histogram():
    with db() as cur:
        cur.execute("""
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
                    WHEN avg_monthly < 50   THEN 0
                    WHEN avg_monthly < 100  THEN 50
                    WHEN avg_monthly < 150  THEN 100
                    WHEN avg_monthly < 200  THEN 150
                    WHEN avg_monthly < 300  THEN 200
                    WHEN avg_monthly < 400  THEN 300
                    WHEN avg_monthly < 500  THEN 400
                    WHEN avg_monthly < 750  THEN 500
                    ELSE 750
                END AS bucket_order,
                COUNT(*) AS count
            FROM apt_spend
            GROUP BY bucket, bucket_order
            ORDER BY bucket_order
        """)
        return [dict(r) for r in cur.fetchall()]


# ── Active subscribers snapshot per month ─────────────────────────────────────

@app.get("/api/funnel/cumulative-subscribers")
def cumulative_subscribers():
    with db() as cur:
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
                TO_CHAR(month_start, 'YYYY-MM') AS sub_month,
                COUNT(*) FILTER (WHERE "NewStatus" IN
                  ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION'))    AS active_subscribers,
                COUNT(*) FILTER (WHERE "NewStatus" = 'RECURRING_SUBSCRIPTION') AS recurring,
                COUNT(*) FILTER (WHERE "NewStatus" = 'ON_DEMAND_SUBSCRIPTION')  AS on_demand
            FROM latest_per_apt_month
            GROUP BY month_start
            ORDER BY month_start
        """)
        return [dict(r) for r in cur.fetchall()]


# ── Visit filter helper (date = visit date, not account date) ─────────────────

def _vextra(date_from=None, date_to=None, project=None, neighbourhood=None, sub_type=None):
    """AND-prefixed fragment for visit-tab filters."""
    clauses, params = [], []
    if date_from:
        clauses.append('v."Date" >= %s')
        params.append(date_from)
    if date_to:
        clauses.append('v."Date" <= %s')
        params.append(date_to)
    if project and project != "all":
        clauses.append('l."Project" = %s')
        params.append(project)
    if neighbourhood and neighbourhood != "all":
        clauses.append('n."Project" = %s')
        params.append(neighbourhood)
    if sub_type == 'recurring':
        clauses.append('a."Status" = \'RECURRING_SUBSCRIPTION\'')
    elif sub_type == 'ondemand':
        clauses.append('a."Status" = \'ON_DEMAND_SUBSCRIPTION\'')
    elif sub_type == 'non_sub':
        clauses.append('a."Status" NOT IN (\'RECURRING_SUBSCRIPTION\',\'ON_DEMAND_SUBSCRIPTION\')')
    sql = ("AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params

# Visits are only considered in these statuses
_V_STATUS = "v.\"Status\" IN ('FINISHED','COMPLETED','PENDING','CANCELLED')"


# ── Visits summary KPIs ────────────────────────────────────────────────────────

@app.get("/api/visits/summary")
def visits_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED')) AS finished_completed,
                COUNT(*) FILTER (WHERE v."Status" = 'FINISHED')               AS finished,
                COUNT(*) FILTER (WHERE v."Status" = 'COMPLETED')              AS completed,
                COUNT(*) FILTER (WHERE v."Status" = 'PENDING')                AS pending,
                COUNT(*) FILTER (WHERE v."Status" = 'CANCELLED')              AS cancelled,
                ROUND(SUM(({_USD_PRICE('v."FinalPrice"')})) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED'))::numeric, 2) AS net_revenue
            FROM "VisitsNew" v
            JOIN  "Apartments"    a ON a."Id" = v."ApartmentId"
            JOIN  "Locations"     l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} AND {_V_STATUS} {extra}
        """, params)
        return dict(cur.fetchone())


# ── Visits time-series (by visit date month) ──────────────────────────────────

@app.get("/api/visits/timeseries")
def visits_timeseries(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COUNT(*) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED')) AS finished_completed,
                COUNT(*) FILTER (WHERE v."Status" = 'PENDING')                AS pending,
                COUNT(*) FILTER (WHERE v."Status" = 'CANCELLED')              AS cancelled,
                ROUND(
                    COUNT(*) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED')) * 100.0
                    / NULLIF(COUNT(*) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED','CANCELLED')), 0),
                1) AS finished_pct,
                ROUND(AVG(v."NetDuration" * 60) FILTER (WHERE v."Status" IN ('FINISHED','COMPLETED') AND v."NetDuration" > 0)::numeric, 0) AS avg_duration_mins
            FROM "VisitsNew" v
            JOIN  "Apartments"    a ON a."Id" = v."ApartmentId"
            JOIN  "Locations"     l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} AND {_V_STATUS} {extra}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """, params)
        return [dict(r) for r in cur.fetchall()]


# ── Chores on visits ───────────────────────────────────────────────────────────

@app.get("/api/visits/chores")
def visits_chores(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        # Summary
        cur.execute(f"""
            SELECT
                COUNT(vt."Id")   AS chore_count,
                ROUND(SUM(t."Price")::numeric, 2) AS total_amount
            FROM "VisitTasks" vt
            JOIN  "VisitsNew"  v  ON v."Id"  = vt."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            JOIN  "Tasks"  t  ON t."Id"  = vt."EntityId"
            WHERE vt."EntityType" = 'task'
              AND {_LOC_GUARD} AND {_V_STATUS} {extra}
        """, params)
        summary = dict(cur.fetchone())

        # Monthly trend: avg tasks per visit + avg visit price
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                ROUND(COUNT(vt."Id")::numeric / NULLIF(COUNT(DISTINCT v."Id"), 0), 2) AS avg_tasks_per_visit,
                ROUND(AVG({_USD_PRICE('v."FinalPrice"')})::numeric, 2) AS avg_visit_price
            FROM "VisitsNew"  v
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            LEFT JOIN "VisitTasks" vt ON vt."VisitId" = v."Id" AND vt."EntityType" = 'task'
            WHERE {_LOC_GUARD} AND {_V_STATUS} {extra}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """, params)
        return {"summary": summary, "timeseries": [dict(r) for r in cur.fetchall()]}


# ── Last Minute Deal visits ────────────────────────────────────────────────────

@app.get("/api/visits/last-minute")
def visits_last_minute(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        # By user type
        cur.execute(f"""
            WITH latest_lmd AS (
                SELECT DISTINCT ON ("EntityId")
                    "EntityId"        AS visit_id,
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
                END                         AS user_type,
                COUNT(DISTINCT lmd.visit_id) AS visit_count
            FROM latest_lmd lmd
            JOIN  "VisitsNew"  v  ON v."Id"  = lmd.visit_id
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} AND {_V_STATUS} {extra}
            GROUP BY user_type
            ORDER BY visit_count DESC
        """, params)
        by_type = [dict(r) for r in cur.fetchall()]

        # Monthly trend
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
            JOIN  "VisitsNew"  v  ON v."Id"  = lmd.visit_id
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} AND {_V_STATUS} {extra}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """, params)
        return {"by_type": by_type, "timeseries": [dict(r) for r in cur.fetchall()]}


# ── Cancellations ──────────────────────────────────────────────────────────────

@app.get("/api/visits/cancellations")
def visits_cancellations(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        # By category (split OOP into Charged vs Free, add cancel_source)
        cur.execute(f"""
            SELECT
                CASE
                    WHEN vc."CancelType" IN ('APP_CANCELLED_OUT_POLICY','ADMIN_CANCELLED_OUT_POLICY')
                        THEN CASE WHEN COALESCE(v."FinalPrice",0) > 0 THEN 'OOP Charged' ELSE 'OOP Free' END
                    WHEN vc."CancelType" IN ('APP_CANCELLED_IN_POLICY','ADMIN_CANCELLED_IN_POLICY')
                        THEN 'In Policy'
                    WHEN vc."CancelType" = 'APP_CANCELLED_IN_FREE_TRIAL'
                        THEN 'From Plan'
                    WHEN vc."CancelType" IN ('ADMIN_FREE_CANCELLATION','APP_FREE_CANCELLATION')
                        THEN 'From Free'
                    ELSE 'Other'
                END AS cancel_category,
                CASE
                    WHEN lower(COALESCE(vc."Comments",'')) SIMILAR TO
                        '%%(duplicate|double.booking|rebook|dbl|dup |dup$|ooc|edit|removed|churn|frozen|reschedul|cancel ahead|no response|ops |admin |bug |scripted|outstanding balance)%%'
                    THEN 'Ops'
                    WHEN vc."CancelType" LIKE 'ADMIN_%%' THEN 'Admin'
                    ELSE 'Customer'
                END AS cancel_source,
                COUNT(*) AS count
            FROM "VisitCancellations" vc
            JOIN  "VisitsNew"  v  ON v."Id"  = vc."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = vc."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE vc."CancelType" NOT IN
              ('APP_RESCHEDULE_NOW','APP_RESCHEDULE_LATER')
              AND {_LOC_GUARD} {extra}
            GROUP BY cancel_category, cancel_source
            ORDER BY count DESC
        """, params)
        by_category = [dict(r) for r in cur.fetchall()]

        # Monthly trend (categories)
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COUNT(*) FILTER (WHERE vc."CancelType" IN
                  ('APP_CANCELLED_OUT_POLICY','ADMIN_CANCELLED_OUT_POLICY'))   AS out_of_policy,
                COUNT(*) FILTER (WHERE vc."CancelType" IN
                  ('APP_CANCELLED_IN_POLICY','ADMIN_CANCELLED_IN_POLICY'))     AS in_policy,
                COUNT(*) FILTER (WHERE vc."CancelType" =
                  'APP_CANCELLED_IN_FREE_TRIAL')                               AS from_plan,
                COUNT(*) FILTER (WHERE vc."CancelType" IN
                  ('ADMIN_FREE_CANCELLATION','APP_FREE_CANCELLATION'))         AS from_free
            FROM "VisitCancellations" vc
            JOIN  "VisitsNew"  v  ON v."Id"  = vc."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = vc."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE vc."CancelType" NOT IN
              ('APP_RESCHEDULE_NOW','APP_RESCHEDULE_LATER')
              AND {_LOC_GUARD} {extra}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """, params)
        timeseries = [dict(r) for r in cur.fetchall()]

        # App vs Admin cancellation monthly timeseries
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COUNT(*) FILTER (WHERE vc."CancelType" LIKE 'APP_%%')   AS app_cancelled,
                COUNT(*) FILTER (WHERE vc."CancelType" LIKE 'ADMIN_%%') AS admin_cancelled
            FROM "VisitCancellations" vc
            JOIN  "VisitsNew"  v  ON v."Id"  = vc."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = vc."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE vc."CancelType" NOT IN
              ('APP_RESCHEDULE_NOW','APP_RESCHEDULE_LATER')
              AND {_LOC_GUARD} {extra}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """, params)
        by_source_ts = [dict(r) for r in cur.fetchall()]

        return {"by_category": by_category, "timeseries": timeseries, "by_source_ts": by_source_ts}


# ── Visit duration histogram ──────────────────────────────────────────────────

@app.get("/api/visits/duration-histogram")
def visits_duration_histogram(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            WITH buckets AS (
                SELECT
                    LEAST(FLOOR(v."NetDuration" * 60 / 30) * 30, 360) AS bucket_min
                FROM "VisitsNew" v
                JOIN  "Apartments"    a ON a."Id" = v."ApartmentId"
                JOIN  "Locations"     l ON l."Id" = a."LocationId"
                LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
                WHERE {_LOC_GUARD}
                  AND v."Status" IN ('FINISHED','COMPLETED')
                  AND v."NetDuration" > 0
                  {extra}
            )
            SELECT
                bucket_min::int            AS bucket_min,
                COUNT(*)                   AS count
            FROM buckets
            GROUP BY bucket_min
            ORDER BY bucket_min
        """, params)
        rows = [dict(r) for r in cur.fetchall()]
    # Build labels like "0-30 min", ..., "360+ min"
    def label(b):
        if b >= 360:
            return "360+ min"
        return f"{b}-{b+30} min"
    return [{"label": label(r["bucket_min"]), "bucket_min": r["bucket_min"], "count": r["count"]} for r in rows]


# ── Task distribution ─────────────────────────────────────────────────────────

@app.get("/api/visits/task-distribution")
def visits_task_distribution(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            SELECT
                t."Title" AS task_name,
                COUNT(*)  AS count
            FROM "VisitTasks" vt
            JOIN  "VisitsNew"  v  ON v."Id"  = vt."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            JOIN  "Tasks"  t  ON t."Id"  = vt."EntityId"
            WHERE vt."EntityType" = 'task'
              AND v."Status" IN ('FINISHED','COMPLETED')
              AND {_LOC_GUARD} {extra}
            GROUP BY t."Title"
            ORDER BY count DESC
            LIMIT 30
        """, params)
        return [dict(r) for r in cur.fetchall()]


# ── No-slot events ────────────────────────────────────────────────────────────

_NS_TYPE = """
    CASE
        WHEN req->>'frequency' IN (
            'once-a-week','twice-a-week','three-a-week','four-times-a-week',
            'every-day','once-in-2-week','once-a-month'
        ) THEN 'Subscription'
        ELSE 'One-time'
    END
"""


def _nsextra(date_from=None, date_to=None, project=None, neighbourhood=None, sub_type=None):
    clauses, params = [], []
    if date_from:
        clauses.append('ns."CreatedAt" >= %s')
        params.append(date_from)
    if date_to:
        clauses.append('ns."CreatedAt" <= %s')
        params.append(date_to)
    if project and project != "all":
        clauses.append('l."Project" = %s')
        params.append(project)
    if neighbourhood and neighbourhood != "all":
        clauses.append('n."Project" = %s')
        params.append(neighbourhood)
    if sub_type == 'recurring':
        clauses.append('a."Status" = \'RECURRING_SUBSCRIPTION\'')
    elif sub_type == 'ondemand':
        clauses.append('a."Status" = \'ON_DEMAND_SUBSCRIPTION\'')
    elif sub_type == 'non_sub':
        clauses.append('a."Status" NOT IN (\'RECURRING_SUBSCRIPTION\',\'ON_DEMAND_SUBSCRIPTION\')')
    sql = ("AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params


@app.get("/api/noslots/summary")
def noslots_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _nsextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            WITH classified AS (
                SELECT
                    ns."ApartmentId",
                    DATE(ns."CreatedAt") AS event_date,
                    {_NS_TYPE}          AS visit_type,
                    AVG(ns."TimeNeeded") FILTER (WHERE ns."TimeNeeded" > 0) AS avg_mins
                FROM "NoSlotEvents" ns
                LEFT JOIN LATERAL jsonb_array_elements(ns."Request") AS req ON true
                JOIN  "Apartments"    a ON a."Id" = ns."ApartmentId"
                JOIN  "Locations"     l ON l."Id" = a."LocationId"
                LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
                WHERE {_LOC_GUARD} {extra}
                GROUP BY ns."ApartmentId", DATE(ns."CreatedAt"), visit_type
            ),
            with_success AS (
                SELECT
                    c.*,
                    EXISTS (
                        SELECT 1 FROM "VisitsNew" v
                        WHERE v."ApartmentId" = c."ApartmentId"
                          AND DATE(v."CreatedAt") = c.event_date
                    ) AS found_slot
                FROM classified c
            )
            SELECT
                COUNT(*)                                              AS total_apt_days,
                COUNT(*) FILTER (WHERE visit_type = 'Subscription')  AS subscription_apt_days,
                COUNT(*) FILTER (WHERE visit_type = 'One-time')      AS onetime_apt_days,
                ROUND(AVG(avg_mins) FILTER (WHERE visit_type = 'Subscription' AND avg_mins IS NOT NULL)::numeric, 0) AS avg_time_sub_mins,
                ROUND(AVG(avg_mins) FILTER (WHERE visit_type = 'One-time'     AND avg_mins IS NOT NULL)::numeric, 0) AS avg_time_onetime_mins,
                SUM(found_slot::int)                                  AS found_slot_count,
                ROUND(SUM(found_slot::int) * 100.0 / NULLIF(COUNT(*), 0), 1) AS success_rate_pct
            FROM with_success
        """, params)
        return dict(cur.fetchone())


@app.get("/api/noslots/timeseries")
def noslots_timeseries(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _nsextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            WITH classified AS (
                SELECT
                    ns."ApartmentId",
                    DATE(ns."CreatedAt")            AS event_date,
                    {_NS_TYPE}                      AS visit_type,
                    AVG(ns."TimeNeeded") FILTER (WHERE ns."TimeNeeded" > 0) AS avg_mins
                FROM "NoSlotEvents" ns
                LEFT JOIN LATERAL jsonb_array_elements(ns."Request") AS req ON true
                JOIN  "Apartments"    a ON a."Id" = ns."ApartmentId"
                JOIN  "Locations"     l ON l."Id" = a."LocationId"
                LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
                WHERE {_LOC_GUARD} {extra}
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
                TO_CHAR(DATE_TRUNC('month', event_date), 'YYYY-MM') AS month,
                COUNT(*) FILTER (WHERE visit_type = 'Subscription')  AS subscription_apt_days,
                COUNT(*) FILTER (WHERE visit_type = 'One-time')      AS onetime_apt_days,
                ROUND(AVG(avg_mins) FILTER (WHERE visit_type = 'Subscription' AND avg_mins IS NOT NULL)::numeric, 0) AS avg_time_sub_mins,
                ROUND(AVG(avg_mins) FILTER (WHERE visit_type = 'One-time'     AND avg_mins IS NOT NULL)::numeric, 0) AS avg_time_onetime_mins,
                ROUND(SUM(found_slot::int) * 100.0 / NULLIF(COUNT(*), 0), 1) AS success_rate_pct
            FROM with_success
            GROUP BY DATE_TRUNC('month', event_date)
            ORDER BY month
        """, params)
        return [dict(r) for r in cur.fetchall()]


# ── Ratings ───────────────────────────────────────────────────────────────────

@app.get("/api/ratings/summary")
def ratings_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            SELECT
                COUNT(r."Id")                                                   AS total_reviews,
                COUNT(r."CustomerRate")                                         AS visit_rating_count,
                ROUND(AVG(r."CustomerRate")::numeric, 2)                        AS avg_visit_rating,
                COUNT(r."FaireeRate")                                           AS app_rating_count,
                ROUND(AVG(r."FaireeRate")::numeric, 2)                         AS avg_app_rating
            FROM "Reviews" r
            JOIN  "VisitsNew"  v  ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} {extra}
        """, params)
        return dict(cur.fetchone())


@app.get("/api/ratings/timeseries")
def ratings_timeseries(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM')   AS month,
                COUNT(r."Id")                                        AS review_count,
                ROUND(AVG(r."CustomerRate")::numeric, 2)             AS avg_visit_rating,
                ROUND(AVG(r."FaireeRate")::numeric, 2)              AS avg_app_rating
            FROM "Reviews" r
            JOIN  "VisitsNew"  v  ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} {extra}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY month
        """, params)
        return [dict(r) for r in cur.fetchall()]


@app.get("/api/ratings/reviews")
def ratings_reviews(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            SELECT
                -- Customer written reviews
                COUNT(*) FILTER (WHERE r."CustomerReview" IS NOT NULL AND r."CustomerReview" != ''
                  AND r."CustomerRate" = 5)                                     AS cust_positive,
                COUNT(*) FILTER (WHERE r."CustomerReview" IS NOT NULL AND r."CustomerReview" != ''
                  AND r."CustomerRate" BETWEEN 3 AND 4)                         AS cust_neutral,
                COUNT(*) FILTER (WHERE r."CustomerReview" IS NOT NULL AND r."CustomerReview" != ''
                  AND r."CustomerRate" < 3)                                     AS cust_negative,
                COUNT(*) FILTER (WHERE r."CustomerReview" IS NOT NULL AND r."CustomerReview" != ''
                  AND r."CustomerRate" IS NULL)                                 AS cust_no_rating,
                -- Fairee written reviews
                COUNT(*) FILTER (WHERE r."FaireeReview" IS NOT NULL AND r."FaireeReview" != ''
                  AND r."FaireeRate" = 5)                                       AS fairee_positive,
                COUNT(*) FILTER (WHERE r."FaireeReview" IS NOT NULL AND r."FaireeReview" != ''
                  AND r."FaireeRate" BETWEEN 3 AND 4)                           AS fairee_neutral,
                COUNT(*) FILTER (WHERE r."FaireeReview" IS NOT NULL AND r."FaireeReview" != ''
                  AND r."FaireeRate" < 3)                                       AS fairee_negative,
                COUNT(*) FILTER (WHERE r."FaireeReview" IS NOT NULL AND r."FaireeReview" != ''
                  AND r."FaireeRate" IS NULL)                                   AS fairee_no_rating
            FROM "Reviews" r
            JOIN  "VisitsNew"  v  ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} {extra}
        """, params)
        return dict(cur.fetchone())


@app.get("/api/ratings/distribution")
def ratings_distribution(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            SELECT
                ROUND(r."CustomerRate"::numeric) AS star,
                COUNT(*) FILTER (WHERE r."CustomerRate" IS NOT NULL) AS visit_count,
                COUNT(*) FILTER (WHERE r."FaireeRate"  IS NOT NULL) AS app_count
            FROM "Reviews" r
            JOIN  "VisitsNew"  v  ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD}
              AND (r."CustomerRate" IS NOT NULL OR r."FaireeRate" IS NOT NULL) {extra}
            GROUP BY ROUND(r."CustomerRate"::numeric)
            ORDER BY star
        """, params)
        rows = [dict(r) for r in cur.fetchall()]

        # Also get app rating distribution separately (FaireeRate may have different star)
        cur.execute(f"""
            SELECT
                ROUND(r."FaireeRate"::numeric) AS star,
                COUNT(*) AS app_count
            FROM "Reviews" r
            JOIN  "VisitsNew"  v  ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} AND r."FaireeRate" IS NOT NULL {extra}
            GROUP BY ROUND(r."FaireeRate"::numeric)
            ORDER BY star
        """, params)
        app_dist = {r["star"]: r["app_count"] for r in cur.fetchall()}

        cur.execute(f"""
            SELECT
                ROUND(r."CustomerRate"::numeric) AS star,
                COUNT(*) AS visit_count
            FROM "Reviews" r
            JOIN  "VisitsNew"  v  ON v."Id"  = r."VisitId"
            JOIN  "Apartments" a  ON a."Id"  = v."ApartmentId"
            JOIN  "Locations"  l  ON l."Id"  = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} AND r."CustomerRate" IS NOT NULL {extra}
            GROUP BY ROUND(r."CustomerRate"::numeric)
            ORDER BY star
        """, params)
        visit_dist = {r["star"]: r["visit_count"] for r in cur.fetchall()}

    stars = [1, 2, 3, 4, 5]
    return {
        "visit": [{"star": s, "count": visit_dist.get(s, 0)} for s in stars],
        "app":   [{"star": s, "count": app_dist.get(s, 0)}   for s in stars],
    }


# ── PostHog: Review Before Booking monthly unique users ───────────────────────

@app.get("/api/posthog/review-views")
async def review_views(date_from: Optional[str] = None, date_to: Optional[str] = None):
    if not PH_KEY:
        return {"error": "POSTHOG_API_KEY not set — set it in .env or environment", "data": []}

    date_clause = ""
    if date_from:
        date_clause += f" AND timestamp >= '{date_from}'"
    if date_to:
        date_clause += f" AND timestamp <= '{date_to}'"

    hogql = f"""
        SELECT
            formatDateTime(toStartOfMonth(timestamp), '%Y-%m') AS month,
            uniqExact(distinct_id)                             AS unique_users
        FROM events
        WHERE event = 'view'
          AND properties.scope = 'subscription-review-before-booking'
          {date_clause}
        GROUP BY toStartOfMonth(timestamp)
        ORDER BY month
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PH_HOST}/api/projects/{PH_PROJECT}/query/",
            headers={"Authorization": f"Bearer {PH_KEY}"},
            json={"query": {"kind": "HogQLQuery", "query": hogql}},
        )
    data = resp.json()
    cols = data.get("columns", [])
    return [dict(zip(cols, row)) for row in data.get("results", [])]


# ── PostHog: Wonderful Signup funnel (day-windowed) ───────────────────────────

@app.get("/api/posthog/signup-funnel")
async def signup_funnel(days: int = 30):
    if not PH_KEY:
        return {"error": "POSTHOG_API_KEY not set", "steps": []}

    hogql = f"""
        SELECT
            uniqExactIf(distinct_id,
                event = 'view' AND properties.scope = 'register-how-many-bedrooms'
                AND properties.$geoip_country_code = 'US') AS view_bedrooms,
            uniqExactIf(distinct_id,
                event = 'view' AND properties.scope = 'register-how-many-bathrooms') AS view_bathrooms,
            uniqExactIf(distinct_id,
                event = 'view' AND properties.scope = 'register-contact-details') AS view_contact,
            uniqExactIf(distinct_id,
                event = 'view' AND properties.scope = 'register-building-details') AS view_building,
            uniqExactIf(distinct_id,
                event = 'register-init' AND properties.isTest != 'true') AS become_lead,
            uniqExactIf(distinct_id,
                event = 'view' AND properties.scope = 'subscription-choose-service') AS choose_service,
            uniqExactIf(distinct_id,
                event = 'view' AND properties.scope = 'subscription-review-before-booking') AS review_booking,
            uniqExactIf(distinct_id,
                event = 'view' AND (properties.scope = 'register-success'
                  OR properties.scope = 'subscription-success')) AS register_success
        FROM events
        WHERE timestamp >= now() - INTERVAL {int(days)} DAY
          AND event IN ('view', 'register-init', 'click')
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PH_HOST}/api/projects/{PH_PROJECT}/query/",
            headers={"Authorization": f"Bearer {PH_KEY}"},
            json={"query": {"kind": "HogQLQuery", "query": hogql}},
        )
    data = resp.json()
    cols = data.get("columns", [])
    rows = data.get("results", [])
    if not rows:
        return {"steps": []}
    row = dict(zip(cols, rows[0]))
    steps = [
        {"step": "View Bedrooms",   "count": row.get("view_bedrooms", 0)},
        {"step": "View Bathrooms",  "count": row.get("view_bathrooms", 0)},
        {"step": "View Contact",    "count": row.get("view_contact", 0)},
        {"step": "View Building",   "count": row.get("view_building", 0)},
        {"step": "Become Lead",     "count": row.get("become_lead", 0)},
        {"step": "Choose Service",  "count": row.get("choose_service", 0)},
        {"step": "Review Booking",  "count": row.get("review_booking", 0)},
        {"step": "Register Success","count": row.get("register_success", 0)},
    ]
    top = steps[0]["count"] or 1
    for s in steps:
        s["pct_of_top"] = round(s["count"] / top * 100, 1)
    return {"steps": steps, "days": days}


# ── Funnel: Residents in Buildings ───────────────────────────────────────────

@app.get("/api/funnel/residents")
def funnel_residents():
    with db() as cur:
        cur.execute("""
            SELECT
                (SELECT COUNT(*) FROM "Apartments" a2
                 JOIN "Locations" l2 ON l2."Id" = a2."LocationId"
                 WHERE l2."IsTest" = false AND l2."Status" = 'ACTIVE') AS total_apartments,
                COALESCE(
                    (SELECT SUM(NULLIF("ApproximateNumberOfApartments", 0))
                     FROM "Locations"
                     WHERE "IsTest" = false AND "Status" = 'ACTIVE'),
                    (SELECT COUNT(*) FROM "Apartments" a2
                     JOIN "Locations" l2 ON l2."Id" = a2."LocationId"
                     WHERE l2."IsTest" = false AND l2."Status" = 'ACTIVE')
                ) AS estimated_residents
        """)
        return dict(cur.fetchone())


# ── Funnel: Conversion source (first subscription event) ─────────────────────

@app.get("/api/funnel/conversion-source-first")
def conversion_source_first(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _extra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        cur.execute(f"""
            WITH first_sub AS (
                SELECT DISTINCT ON ("ApartmentId")
                    "ApartmentId",
                    "NewStatus",
                    "ChangedBy"
                FROM "ApartmentStatusHistory"
                WHERE "NewStatus" IN ('RECURRING_SUBSCRIPTION','ON_DEMAND_SUBSCRIPTION')
                  AND "ChangedBy" IS NOT NULL AND "ChangedBy" != 0
                ORDER BY "ApartmentId", "CreatedAt" ASC
            )
            SELECT
                CASE
                    WHEN self_cust."AccountId" IS NOT NULL THEN 'Self'
                    ELSE 'Admin'
                END                     AS converted_by,
                fs."NewStatus"          AS subscription_type,
                COUNT(*)                AS count
            FROM first_sub fs
            JOIN  "Apartments"    a         ON a."Id"            = fs."ApartmentId"
            LEFT JOIN "Customers" self_cust ON self_cust."ApartmentId" = fs."ApartmentId"
              AND self_cust."AccountId" = fs."ChangedBy"
            LEFT JOIN "Customers" c         ON c."ApartmentId"  = a."Id"
            LEFT JOIN "Accounts"  acc       ON acc."Id"          = c."AccountId"
            JOIN  "Locations"     l         ON l."Id"            = a."LocationId"
            LEFT JOIN "Neighborhoods" n     ON n."Id"            = a."NeighborhoodId"
            WHERE {_LOC_GUARD} {extra}
            GROUP BY converted_by, fs."NewStatus"
            ORDER BY converted_by, fs."NewStatus"
        """, params)
        return [dict(r) for r in cur.fetchall()]


# ── Visits: Bookings by source (App vs Admin) ─────────────────────────────────

@app.get("/api/visits/bookings-by-source")
def visits_bookings_by_source(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _vextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        # Summary
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
            JOIN "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN "Locations" l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} AND v."Status" IN ('FINISHED','COMPLETED') {extra}
            GROUP BY booked_by
        """, params)
        summary = [dict(r) for r in cur.fetchall()]

        # Monthly timeseries
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', v."Date"), 'YYYY-MM') AS visit_month,
                COUNT(*) FILTER (WHERE v."ChangedByAccount" IS NULL OR EXISTS (
                    SELECT 1 FROM "Customers" c WHERE c."ApartmentId" = v."ApartmentId" AND c."AccountId" = v."ChangedByAccount"
                )) AS app_booked,
                COUNT(*) FILTER (WHERE v."ChangedByAccount" IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM "Customers" c WHERE c."ApartmentId" = v."ApartmentId" AND c."AccountId" = v."ChangedByAccount"
                )) AS admin_booked
            FROM "VisitsNew" v
            JOIN "Apartments" a ON a."Id" = v."ApartmentId"
            JOIN "Locations" l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} AND v."Status" IN ('FINISHED','COMPLETED') {extra}
            GROUP BY DATE_TRUNC('month', v."Date")
            ORDER BY visit_month
        """, params)
        return {"summary": summary, "timeseries": [dict(r) for r in cur.fetchall()]}


# ── No Slots: Incidents (raw events count) ────────────────────────────────────

@app.get("/api/noslots/incidents")
def noslots_incidents(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    sub_type: Optional[str] = None,
):
    extra, params = _nsextra(date_from, date_to, project, neighbourhood, sub_type)
    with db() as cur:
        # Summary
        cur.execute(f"""
            SELECT
                COUNT(*) AS total_incidents,
                COUNT(*) FILTER (WHERE {_NS_TYPE} = 'Subscription') AS sub_incidents,
                COUNT(*) FILTER (WHERE {_NS_TYPE} = 'One-time') AS onetime_incidents
            FROM "NoSlotEvents" ns
            LEFT JOIN LATERAL jsonb_array_elements(ns."Request") AS req ON true
            JOIN "Apartments" a ON a."Id" = ns."ApartmentId"
            JOIN "Locations" l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} {extra}
        """, params)
        summary = dict(cur.fetchone())

        # Monthly timeseries
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', ns."CreatedAt"), 'YYYY-MM') AS month,
                COUNT(*) AS total_incidents,
                COUNT(*) FILTER (WHERE {_NS_TYPE} = 'Subscription') AS sub_incidents,
                COUNT(*) FILTER (WHERE {_NS_TYPE} = 'One-time') AS onetime_incidents
            FROM "NoSlotEvents" ns
            LEFT JOIN LATERAL jsonb_array_elements(ns."Request") AS req ON true
            JOIN "Apartments" a ON a."Id" = ns."ApartmentId"
            JOIN "Locations" l ON l."Id" = a."LocationId"
            LEFT JOIN "Neighborhoods" n ON n."Id" = a."NeighborhoodId"
            WHERE {_LOC_GUARD} {extra}
            GROUP BY DATE_TRUNC('month', ns."CreatedAt")
            ORDER BY month
        """, params)
        return {"summary": summary, "timeseries": [dict(r) for r in cur.fetchall()]}


# ── Serve frontend ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.get("/")
def index():
    return FileResponse(str(HERE / "static" / "index.html"))
