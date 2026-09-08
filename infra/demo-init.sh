#!/bin/sh
# Runs only for a fresh demo Postgres volume.  The demo source is isolated from
# normal local data, and all credentials below are local fixture credentials.
set -eu

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<'SQL'
CREATE DATABASE northwind;
CREATE DATABASE steward;
CREATE ROLE ops_reader LOGIN PASSWORD 'Opsread7';
CREATE ROLE crm_reader LOGIN PASSWORD 'Crmro88';
-- Trino's read-only source catalogs use a separate account from the
-- human-provided connection used by the Agent.  Keeping it explicit makes
-- the bronze writer independent of the rehearsal email credentials.
CREATE ROLE agent_ro LOGIN PASSWORD 'ro_pass';
SQL

psql --username "$POSTGRES_USER" --dbname northwind --set ON_ERROR_STOP=1 \
  -f /demo-seed/northwind.sql

psql --username "$POSTGRES_USER" --dbname northwind --set ON_ERROR_STOP=1 <<'SQL'
GRANT CONNECT ON DATABASE northwind TO ops_reader, crm_reader, agent_ro;
GRANT USAGE ON SCHEMA public TO ops_reader, crm_reader, agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ops_reader, agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ops_reader, agent_ro;

-- A deliberately dirty, demo-only feed.  It never alters the imported
-- Northwind tables and gives the silver page real before/after evidence.
CREATE TABLE demo_order_status AS
SELECT order_id,
       CASE (order_id % 4)
         WHEN 0 THEN ' DELIVERED '
         WHEN 1 THEN 'Delivered'
         WHEN 2 THEN 'delivered'
         ELSE 'PENDING '
       END AS order_status
FROM orders
WHERE order_id IN (SELECT order_id FROM orders ORDER BY order_id LIMIT 24);
GRANT SELECT ON demo_order_status TO ops_reader, agent_ro;

-- A second immutable fixture gives the presentation page a real clean-source
-- snapshot without ever rewriting the dirty fixture or Northwind tables.
CREATE TABLE demo_order_status_clean AS
SELECT order_id, trim(upper(order_status)) AS order_status
FROM demo_order_status;
GRANT SELECT ON demo_order_status_clean TO ops_reader, agent_ro;
SQL

psql --username "$POSTGRES_USER" --dbname steward --set ON_ERROR_STOP=1 \
  --set agent_password=agent_pass --set approver_password=approver_pass \
  -f /demo-seed/init-steward.sql
