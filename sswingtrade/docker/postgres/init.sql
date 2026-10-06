-- PostgreSQL Initialization Script for SSWingTrade
-- Runs automatically when Docker starts postgres container

-- TimescaleDB is NOT enabled: the stock postgres:16-alpine image does not ship it, so CREATE EXTENSION would abort
-- the container's first start. To opt in, switch the compose image to timescale/timescaledb:latest-pg16 and uncomment:
-- CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Create schema
CREATE SCHEMA IF NOT EXISTS trading;

-- Set search path
SET search_path TO trading, public;

-- Log initialization
SELECT 'SSWingTrade database initialized' as status;
