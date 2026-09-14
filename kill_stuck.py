#!/usr/bin/env python3
"""Encerra conexoes PostgreSQL travadas.

Regras:
  - COMMIT/ROLLBACK ha mais de 3 minutos: termina (inclusive se o painel mostrar idle)
  - idle in transaction parada ha mais de 3 minutos (sem query nova): termina
  - qualquer outra query ativa ha mais de 5 minutos: termina
  - transacao longa com queries recentes nao e encerrada
  - conexoes idle (sem transacao aberta) nao sao encerradas
  - nunca encerra o proprio backend nem workers internos

Uso:
  python3 kill_stuck.py --dry-run
  python3 kill_stuck.py
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

COMMIT_ROLLBACK_SECONDS = 3 * 60
OTHER_ACTIVE_SECONDS = 5 * 60

COMMIT_ROLLBACK_PREFIXES = ("COMMIT", "ROLLBACK", "ABORT", "END")


@dataclass(frozen=True)
class Connection:
    pid: int
    datname: str | None
    usename: str | None
    application_name: str | None
    client_addr: str | None
    state: str | None
    backend_type: str | None
    query: str | None
    query_age_seconds: float | None
    xact_age_seconds: float | None
    state_age_seconds: float | None = None
    is_self: bool = False


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def is_commit_or_rollback(query: str | None) -> bool:
    if not query:
        return False
    text = query.strip().lstrip("(").upper()
    return text.startswith(COMMIT_ROLLBACK_PREFIXES)


def should_terminate(conn: Connection) -> bool:
    if conn.is_self:
        return False
    if conn.backend_type and conn.backend_type != "client backend":
        return False

    query_age = conn.query_age_seconds or 0
    state_age = conn.state_age_seconds or 0

    if is_commit_or_rollback(conn.query):
        return query_age >= COMMIT_ROLLBACK_SECONDS

    if not conn.state or conn.state == "idle":
        return False

    if conn.state.startswith("idle in transaction"):
        return state_age >= COMMIT_ROLLBACK_SECONDS

    if conn.state == "active":
        return query_age >= OTHER_ACTIVE_SECONDS

    return False


def connect():
    try:
        import psycopg2
    except ImportError:
        sys.exit("Instale as dependencias: pip install -r requirements.txt")

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)

    host = os.environ.get("PGHOST")
    user = os.environ.get("PGUSER")
    password = os.environ.get("PGPASSWORD")
    dbname = os.environ.get("PGDATABASE")
    if not all([host, user, password, dbname]):
        sys.exit(
            "Defina DATABASE_URL ou PGHOST, PGPORT, PGUSER, PGPASSWORD e PGDATABASE "
            "(veja .env.example)."
        )

    return psycopg2.connect(
        host=host,
        port=os.environ.get("PGPORT", "5432"),
        user=user,
        password=password,
        dbname=dbname,
        sslmode=os.environ.get("PGSSLMODE", "prefer"),
    )


def fetch_connections(cursor) -> list[Connection]:
    cursor.execute(
        """
        SELECT
            pid,
            datname,
            usename,
            application_name,
            host(client_addr) AS client_addr,
            state,
            backend_type,
            query,
            EXTRACT(EPOCH FROM (now() - query_start)) AS query_age_seconds,
            EXTRACT(EPOCH FROM (now() - xact_start)) AS xact_age_seconds,
            EXTRACT(EPOCH FROM (now() - state_change)) AS state_age_seconds,
            pid = pg_backend_pid() AS is_self
        FROM pg_stat_activity
        """
    )
    rows = []
    for row in cursor.fetchall():
        rows.append(
            Connection(
                pid=row[0],
                datname=row[1],
                usename=row[2],
                application_name=row[3],
                client_addr=row[4],
                state=row[5],
                backend_type=row[6],
                query=row[7],
                query_age_seconds=row[8],
                xact_age_seconds=row[9],
                state_age_seconds=row[10],
                is_self=bool(row[11]),
            )
        )
    return rows


def format_query(query: str | None, limit: int = 80) -> str:
    if not query:
        return ""
    text = " ".join(query.split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def describe(conn: Connection) -> str:
    if conn.state and conn.state.startswith("idle in transaction"):
        age = conn.state_age_seconds
        age_label = "idle_for"
    else:
        age = conn.query_age_seconds
        age_label = "query_age"
    return (
        f"pid={conn.pid} db={conn.datname} app={conn.application_name} "
        f"state={conn.state} {age_label}={format_age(age)} "
        f"xact_age={format_age(conn.xact_age_seconds)} query={format_query(conn.query)!r}"
    )


def terminate(cursor, pid: int) -> bool:
    cursor.execute("SELECT pg_terminate_backend(%s)", (pid,))
    row = cursor.fetchone()
    return bool(row and row[0])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Encerra conexoes PostgreSQL travadas.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas lista o que seria encerrado, sem terminar nada.",
    )
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).with_name(".env")),
        help="Arquivo .env com as credenciais (padrao: .env neste diretorio).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file))

    connection = connect()
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            connections = fetch_connections(cursor)
            targets = [item for item in connections if should_terminate(item)]

            if not targets:
                print("Nenhuma conexao travada encontrada.")
                return 0

            action = "DRY-RUN" if args.dry_run else "TERMINATE"
            for item in targets:
                print(f"[{action}] {describe(item)}")
                if not args.dry_run:
                    ok = terminate(cursor, item.pid)
                    print(f"  -> {'encerrada' if ok else 'falhou'}")
            return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
