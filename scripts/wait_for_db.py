import os
import time

import psycopg


def wait_for_database():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    for _ in range(30):
        try:
            with psycopg.connect(database_url):
                return
        except psycopg.OperationalError:
            time.sleep(1)

    raise RuntimeError("Database did not become available within 30 seconds")


if __name__ == "__main__":
    wait_for_database()
