-- Runs once on first container start. Migrations also CREATE EXTENSION IF NOT EXISTS.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE DATABASE jarvis_test;
\connect jarvis_test
CREATE EXTENSION IF NOT EXISTS vector;
