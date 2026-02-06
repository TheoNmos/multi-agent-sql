#!/bin/bash

# Setup script for bird database
# This script creates the bird database and runs the BIRD_dev.sql file

set -e

DB_NAME="passarinho"
DB_USER="xiaolongli"
DB_PASSWORD="postgres"
DB_HOST="localhost"
DB_PORT="5432"
SQL_FILE="./datasets/bird/BIRD_dev.sql"

echo "Setting up bird database..."

# Check if SQL file exists
if [ ! -f "$SQL_FILE" ]; then
    echo "❌ Error: SQL file not found at $SQL_FILE"
    echo "Please ensure the BIRD_dev.sql file exists in the expected location."
    exit 1
fi

# Export password for psql
export PGPASSWORD="$DB_PASSWORD"

# Create database if it doesn't exist
echo "Creating database $DB_NAME if it doesn't exist..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1 || \
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c "CREATE DATABASE $DB_NAME;"

# Run the BIRD_dev.sql file
echo "Running BIRD_dev.sql..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f "$SQL_FILE"

echo "✅ bird database setup complete!"

# Unset password
unset PGPASSWORD
