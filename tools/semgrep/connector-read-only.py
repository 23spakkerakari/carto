# Semgrep self-test fixture for connector-read-only.yaml (spec 18.3: "Semgrep rule self-test").
# `semgrep --test tools/semgrep` checks that every `ruleid:` line matches and every `ok:` line does not.
# This file is excluded from ruff, mypy, bandit and the real Semgrep scan.

import os
import shutil

import sqlalchemy
import sqlalchemy as sa
from sqlalchemy import text


async def sftp(client, path):
    # ok: carto-connector-read-only
    await client.listdir(path)
    # ok: carto-connector-read-only
    await client.stat(path)
    # ruleid: carto-connector-read-only
    await client.put("local.csv", path)
    # ruleid: carto-connector-read-only
    await client.remove(path)
    # ruleid: carto-connector-read-only
    await client.rename(path, path + ".done")
    # ruleid: carto-connector-read-only
    await client.mkdir(path)


def http(session, url):
    # ok: carto-connector-read-only
    session.get(url)
    # ok: carto-connector-read-only
    session.head(url)
    # ok: carto-connector-read-only
    session.request("GET", url)
    # ruleid: carto-connector-read-only
    session.post(url, data={})
    # ruleid: carto-connector-read-only
    session.delete(url)
    # ruleid: carto-connector-read-only
    session.request("PUT", url)


def sql(cursor):
    # ok: carto-connector-read-only
    cursor.execute("SELECT id FROM purchase_orders WHERE updated_at > :watermark")
    # ok: carto-connector-read-only
    cursor.execute("WITH recent AS (SELECT 1) SELECT * FROM recent")
    # ruleid: carto-connector-read-only
    cursor.execute("UPDATE purchase_orders SET status = 'SHIPPED'")
    # ruleid: carto-connector-read-only
    cursor.execute("delete from purchase_orders")
    # ruleid: carto-connector-read-only
    cursor.executemany("INSERT INTO t VALUES (?)", [(1,)])


async def sftp_files(sftp, path):
    # ok: carto-connector-read-only
    await sftp.readdir(path)
    # ok: carto-connector-read-only
    async with sftp.open(path, "rb") as handle:
        await handle.read()
    # ok: carto-connector-read-only
    async with sftp.open(path, mode="r") as handle:
        await handle.read()
    # ruleid: carto-connector-read-only
    async with sftp.open(path, "w") as handle:
        await handle.write("x")
    # ruleid: carto-connector-read-only
    async with sftp.open(path, "wb") as handle:
        await handle.write(b"x")
    # ruleid: carto-connector-read-only
    async with sftp.open(path, "a") as handle:
        await handle.write("x")
    # ruleid: carto-connector-read-only
    async with sftp.open(path, mode="r+") as handle:
        await handle.write("x")


async def ssh(connection):
    # ok: carto-connector-read-only
    async with connection.start_sftp_client() as sftp:
        await sftp.readdir(".")
    # ruleid: carto-connector-read-only
    await connection.run("rm -rf /data")
    # ruleid: carto-connector-read-only
    await connection.create_process("touch marker")
    # ruleid: carto-connector-read-only
    await connection.create_subprocess(None, "touch marker")


def local_files(path, other):
    # ok: carto-connector-read-only
    with path.open("rb") as handle:
        handle.read()
    # ok: carto-connector-read-only
    with open(path, "rb") as handle:
        handle.read()
    # ruleid: carto-connector-read-only
    with path.open("w") as handle:
        handle.write("x")
    # ruleid: carto-connector-read-only
    with path.open(mode="ab") as handle:
        handle.write(b"x")
    # ruleid: carto-connector-read-only
    with open(path, "wb") as handle:
        handle.write(b"x")
    # ruleid: carto-connector-read-only
    with open(path, mode="x") as handle:
        handle.write("x")
    # ruleid: carto-connector-read-only
    os.replace(path, other)
    # ruleid: carto-connector-read-only
    os.remove(path)
    # ruleid: carto-connector-read-only
    shutil.move(path, other)
    # ruleid: carto-connector-read-only
    shutil.copy2(path, other)


def httpx_client(client, url):
    # ok: carto-connector-read-only
    client.build_request("GET", url, params={"output_mode": "json"})
    # ok: carto-connector-read-only
    client.build_request("HEAD", url)
    # ok: carto-connector-read-only
    client.build_request(method="GET", url=url)
    # ruleid: carto-connector-read-only
    client.build_request("POST", url, data={"search": "x"})
    # ruleid: carto-connector-read-only
    client.build_request("PUT", url)
    # ruleid: carto-connector-read-only
    client.build_request("PATCH", url)
    # ruleid: carto-connector-read-only
    client.build_request("DELETE", url)
    # ruleid: carto-connector-read-only
    client.build_request(method="POST", url=url)
    # ruleid: carto-connector-read-only
    client.request(method="DELETE", url=url)


def sqlalchemy_core(connection, session, sql):
    # ok: carto-connector-read-only
    connection.execute(text(sql), {"watermark": 1})
    # ok: carto-connector-read-only
    connection.execute(text("SELECT id FROM purchase_orders WHERE updated_at > :watermark"))
    # ok: carto-connector-read-only
    connection.execute(sa.text("WITH recent AS (SELECT 1) SELECT * FROM recent"))
    # ok: carto-connector-read-only
    session.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
    # ok: carto-connector-read-only
    session.exec_driver_sql("START TRANSACTION READ ONLY")
    # ok: carto-connector-read-only
    session.exec_driver_sql("SELECT CURRENT_TIMESTAMP")
    # ruleid: carto-connector-read-only
    connection.execute(text("UPDATE purchase_orders SET status = 'SHIPPED'"))
    # ruleid: carto-connector-read-only
    connection.execute(sa.text("delete from purchase_orders"))
    # ruleid: carto-connector-read-only
    connection.execute(sqlalchemy.text("INSERT INTO t VALUES (1)"))
    # ruleid: carto-connector-read-only
    session.exec_driver_sql("insert into t values (1)")
    # ruleid: carto-connector-read-only
    session.exec_driver_sql("  DROP TABLE t")
    # ruleid: carto-connector-read-only
    session.exec_driver_sql("EXEC sp_who")
