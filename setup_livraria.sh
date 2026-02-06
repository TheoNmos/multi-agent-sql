#!/bin/bash

# Setup script for livraria database
# This script creates the livraria database and runs the schema creation SQL

set -e

DB_NAME="livraria"
DB_USER="xiaolongli"
DB_PASSWORD="postgres"
DB_HOST="localhost"
DB_PORT="5432"
SCHEMA_FILE="./datasets/livraria/create_schema.sql"
DATA_FILE="./datasets/livraria/insert_data.sql"

echo "Setting up livraria database..."

# Export password for psql
export PGPASSWORD="$DB_PASSWORD"

# Drop database if it exists (for clean setup)
echo "Dropping database $DB_NAME if it exists..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres <<EOF 2>/dev/null || true
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS $DB_NAME;
EOF

# Create database
echo "Creating database $DB_NAME..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c "CREATE DATABASE $DB_NAME;"

# Run the schema creation SQL
echo "Running schema creation SQL..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f "$SCHEMA_FILE"

# Run the insert data SQL
echo "Inserting data..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f "$DATA_FILE"

echo "✅ livraria database setup complete!"

# Unset password
unset PGPASSWORD
