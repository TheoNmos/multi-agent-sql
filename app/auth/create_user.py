from __future__ import annotations

import argparse
import asyncio

from app.auth import db as auth_db
from app.config import auth_settings


async def _create_user(username: str, password: str, *, inactive: bool) -> None:
    from app.db.metadata import init_metadata_db, is_metadata_db_ready

    await init_metadata_db()
    if not is_metadata_db_ready():
        raise SystemExit("ANALYTICS_DATABASE_URL is not configured or metadata database failed to initialize")

    existing = await auth_db.get_user_by_username(username)
    if existing is not None:
        raise SystemExit(f"User already exists: {username}")

    user = await auth_db.create_user(username, password, is_active=not inactive)
    print(f"Created user {user.username} ({user.id})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an application user")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--inactive", action="store_true", help="Create the user as inactive")
    args = parser.parse_args()

    if auth_settings.app_secret_key == "change-me-in-production-use-openssl-rand-base64-32":
        print("Warning: APP_SECRET_KEY is using the default value")

    asyncio.run(_create_user(args.username, args.password, inactive=args.inactive))


if __name__ == "__main__":
    main()
