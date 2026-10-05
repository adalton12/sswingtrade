-- PostgreSQL Initialization Script for SSWingTrade
-- Runs automatically when Docker starts postgres container

-- Enable TimescaleDB extension for time-series optimization
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Create schema
CREATE SCHEMA IF NOT EXISTS trading;

-- Set search path
SET search_path TO trading, public;

-- Log initialization
SELECT 'SSWingTrade database initialized' as status;
