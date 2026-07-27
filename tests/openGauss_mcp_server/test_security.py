"""Regression tests for the four reported database security issues."""

from unittest.mock import MagicMock, patch

import pytest
from psycopg2 import ProgrammingError
from pydantic import AnyUrl, TypeAdapter

import openGauss_mcp_server.server as server


@pytest.fixture
def mock_database():
    with (
        patch.object(server, "get_db_config", return_value={}),
        patch.object(server, "connect") as connect,
    ):
        connection = MagicMock()
        cursor = MagicMock()
        connect.return_value.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = ("existing_index",)
        cursor.description = [("id",), ("score",)]
        cursor.fetchall.return_value = []
        yield connect, cursor


def resource_uri(value: str) -> AnyUrl:
    return TypeAdapter(AnyUrl).validate_python(value)


@pytest.mark.asyncio
async def test_read_resource_rejects_injected_table_name(mock_database):
    connect, _ = mock_database

    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        await server.read_resource(
            resource_uri("opengauss://users;select(pg_sleep(1));--/data")
        )

    connect.assert_not_called()


@pytest.mark.asyncio
async def test_read_resource_does_not_expose_database_error(mock_database):
    _, cursor = mock_database
    secret = "private_schema.customers"
    cursor.execute.side_effect = ProgrammingError(f'relation "{secret}" does not exist')

    with pytest.raises(RuntimeError) as raised:
        await server.read_resource(resource_uri("opengauss://customers/data"))

    assert str(raised.value) == "Database operation failed"
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"table_name": "docs;drop table users;--"},
        {"full_text_search_column_name": ["body);drop table users;--"]},
        {"output_column_name": ["id);drop table users;--"]},
    ],
)
@pytest.mark.asyncio
async def test_fulltext_search_rejects_injected_identifiers(mock_database, kwargs):
    connect, _ = mock_database
    arguments = {
        "table_name": "docs",
        "full_text_search_column_name": ["body"],
        "keyword": "safe",
        **kwargs,
    }

    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        await server.fulltext_search(**arguments)

    connect.assert_not_called()


@pytest.mark.asyncio
async def test_fulltext_search_parameterizes_structured_filters(mock_database):
    _, cursor = mock_database

    await server.fulltext_search(
        table_name="docs",
        full_text_search_column_name=["body"],
        keyword="safe",
        other_where_clause=[
            {"column": "tenant_id", "operator": "=", "value": "tenant-a"}
        ],
        limit=3,
        output_column_name=["id"],
    )

    statement, parameters = cursor.execute.call_args.args
    assert statement == (
        'set enable_seqscan = off; set enable_indexscan = on;'
        'SELECT "id", "body" <&> %s as score FROM "docs" '
        'WHERE "tenant_id" = %s ORDER BY "body" <&> %s desc limit %s'
    )
    assert parameters == ["safe", "tenant-a", "safe", 3]


@pytest.mark.asyncio
async def test_fulltext_search_rejects_raw_sql_filter(mock_database):
    connect, _ = mock_database

    with pytest.raises(ValueError, match="Raw SQL filters are no longer supported"):
        await server.fulltext_search(
            table_name="docs",
            full_text_search_column_name=["body"],
            keyword="safe",
            other_where_clause=["tenant_id = 1 OR 1=1"],
        )

    connect.assert_not_called()


def test_ensure_bm25_index_quotes_identifiers(mock_database):
    _, cursor = mock_database
    cursor.fetchone.side_effect = [None, ("bm25_public_docs_body",)]

    server.ensure_bm25_index(cursor, "public.docs", "body")

    assert cursor.execute.call_args_list[1].args[0] == (
        'CREATE INDEX "bm25_public_docs_body" ON "public"."docs" USING bm25("body");'
    )


@pytest.mark.asyncio
async def test_fulltext_search_does_not_expose_database_error(mock_database):
    _, cursor = mock_database
    secret = "private_schema.password"
    cursor.execute.side_effect = ProgrammingError(f'column "{secret}" does not exist')

    result = await server.fulltext_search("docs", ["body"], "safe")

    assert result == "Error executing query"
    assert secret not in result
