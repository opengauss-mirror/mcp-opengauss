import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch
from urllib import error

import psycopg2
import psycopg2.extras
import pytest

import openGauss_mcp_server.server as server_module
from openGauss_mcp_server.server import (
    app,
    call_tool,
    create_fulltext_index,
    create_vector_index,
    credential_cache,
    fulltext_search,
    get_db_config,
    hybrid_search,
    list_resources,
    list_tools,
    main,
    multi_vector_search,
    normalize_vector_scores,
    read_resource,
    search_opengauss_document,
    vector_search,
)

@pytest.fixture(autouse=True)
def setup_env():
    credential_cache.clear_cache()
    os.environ["OPENGAUSS_USER"] = "test_user"
    os.environ["OPENGAUSS_PASSWORD"] = "test_pwd"
    os.environ["OPENGAUSS_DBNAME"] = "test_db"
    os.environ["OPENGAUSS_PORT"] = "5432"
    yield
    credential_cache.clear_cache()
    for key in ["OPENGAUSS_USER", "OPENGAUSS_PASSWORD", "OPENGAUSS_DBNAME", "OPENGAUSS_PORT"]:
        os.environ.pop(key, None)

@pytest.fixture
def mock_db_connection():
    with patch("openGauss_mcp_server.server.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        yield mock_connect, mock_cursor

def test_server_initialization():
    """Test that the server initializes correctly."""
    assert app.name == "openGauss_mcp_server"


@pytest.mark.asyncio
async def test_list_tools():
    """Test that list_tools returns expected tools."""
    tools = await list_tools()

    assert len(tools) == 6
    assert tools[0].name == "execute_sql"
    assert "query" in tools[0].inputSchema["properties"]


@pytest.mark.asyncio
async def test_call_tool_invalid_name():
    """Test calling a tool with an invalid name."""
    with pytest.raises(ValueError, match="Unknown tool"):
        await call_tool("invalid_tool", {})


@pytest.mark.asyncio
async def test_call_tool_missing_query():
    """Test calling execute_sql without a query."""
    with pytest.raises(ValueError, match="Query is required"):
        await call_tool("execute_sql", {})


# Skip database-dependent tests if no database connection
@pytest.mark.asyncio
@pytest.mark.skipif(
    not all([
        pytest.importorskip("psycopg2"),
        pytest.importorskip("openGauss_mcp_server")
    ]),
    reason="openGauss connection not available"
)
async def test_list_resources():
    """Test listing resources (requires database connection)."""
    try:
        resources = await list_resources()
        assert isinstance(resources, list)
    except ValueError as e:
        if "Missing required database configuration" in str(e):
            pytest.skip("Database configuration not available")
        raise

def test_normalize_vector_scores():
    # cos_distance
    cos_distances = [0.1, 0.3, 0.5]
    cos_norm = normalize_vector_scores(cos_distances, "cosine")
    assert cos_norm == [(2 - d) / 2 for d in cos_distances]
    assert cos_norm == [0.95, 0.85, 0.75]

    # l2_distance
    l2_distances = [1.0, 2.0, 3.0]
    l2_norm = normalize_vector_scores(l2_distances, "l2")
    assert l2_norm == [1.0, 0.5, 0.0]

    # ip_distance
    ip_distances = [-0.8, -0.5, -0.2]
    ip_norm = normalize_vector_scores(ip_distances, "ip")
    true_inner_products = [-d for d in ip_distances]
    max_ip, min_ip = max(true_inner_products), min(true_inner_products)
    expected_ip = [(ip - min_ip) / (max_ip - min_ip) for ip in true_inner_products]
    assert ip_norm == expected_ip
    assert ip_norm == pytest.approx([1.0, 0.5, 0.0])

@pytest.mark.asyncio
async def test_fulltext_search(mock_db_connection):
    mock_connect, mock_cursor = mock_db_connection
    mock_cursor.fetchone.side_effect = [("bm25_table1_col1",), None]
    mock_cursor.description = [("id",), ("content",), ("score",)]
    mock_cursor.fetchall.return_value = [(1, "test content", 0.9), (2, "demo content", 0.8)]
    
    result = await fulltext_search(
        table_name="table1",
        full_text_search_column_name=["col1"],
        keyword="test",
        limit=2
    )
    assert len(result) == 2
    assert result[0]["id"] == 1
    assert result[0]["score"] == 0.9

@pytest.mark.asyncio
async def test_vector_search(mock_db_connection):
    mock_connect, mock_cursor = mock_db_connection
    mock_cursor.description = [("id",), ("vector",), ("score",)]
    mock_cursor.fetchall.return_value = [(1, "[0.1,0.2]", 0.1), (2, "[0.3,0.4]", 0.2)]
    
    result = await vector_search(
        table_name="table1",
        vector_data="[0.1,0.2]",
        vec_column_name="vector",
        distance_func="cosine",
        topk=2
    )
    assert len(result) == 2
    assert result[0]["score"] == 0.1

    with pytest.raises(ValueError, match="Unsupported distance_func"):
        await vector_search(
            table_name="table1",
            vector_data="[0.1,0.2]",
            distance_func="euclidean"
        )

@pytest.mark.asyncio
async def test_hybrid_search(mock_db_connection):
    mock_connect, mock_cursor = mock_db_connection
    bm25_mock = AsyncMock(return_value=[
        {"id": 1, "content": "test", "score": 0.8},
        {"id": 2, "content": "demo", "score": 0.6}
    ])
    vector_mock = AsyncMock(return_value=[
        {"id": 1, "content": "test", "score": 0.1},
        {"id": 3, "content": "new", "score": 0.2}
    ])

    with patch("openGauss_mcp_server.server.fulltext_search", bm25_mock), patch("openGauss_mcp_server.server.vector_search", vector_mock):
        result = await hybrid_search(
            table_name="table1",
            full_text_search_column_name=["col1"],
            keyword="test",
            vector_data="[0.1,0.2]",
            text_weight=0.5,
            vector_weight=0.5
        )
        assert len(result) == 3 
        assert result[0]["hybrid_score"] == 0.975


@pytest.mark.asyncio
async def test_search_opengauss_document():
    with patch("openGauss_mcp_server.server.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "obj": {"records": [{"path": "docs/test", "title": "Test Doc"}]}
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with patch("openGauss_mcp_server.server.get_opengauss_doc_content") as mock_get_content:
            mock_get_content.return_value = {
                "title": "Test Doc",
                "url": "https://docs.opengauss.org/docs/test.html",
                "content": "Test content"
            }
            result = await search_opengauss_document(keyword="测试")
            assert "Test content" in result

    with patch("openGauss_mcp_server.server.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = error.HTTPError(
            url="https://docs.opengauss.org/api/search",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None
        )
        result = await search_opengauss_document(keyword="测试")
        
        assert result == "No results were found"

@pytest.mark.asyncio
@pytest.mark.skipif(
    not hasattr(server_module, "OGMemory"),
    reason="Memory tools are disabled unless ENABLE_MEMORY is set",
)
async def test_og_memory():
    with patch("openGauss_mcp_server.server.OGMemory") as mock_og_memory:
        mock_og_memory_instance = MagicMock()
        mock_og_memory_instance.get_embedding_context.return_value = [0.1, 0.2]
        mock_og_memory.return_value = mock_og_memory_instance

        with patch("openGauss_mcp_server.server.vector_search") as mock_vector_search:
            mock_vector_search.return_value = [
                {"mem_id": 1, "content": "User likes coffee"}
            ]
            result = await server_module.og_memory_query(query="preference", topk=2)
            assert result[0]["mem_id"] == 1
            assert result[0]["content"] == "User likes coffee"

        with patch("openGauss_mcp_server.server.connect") as mock_connect:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_connect.return_value.__enter__.return_value = mock_conn
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            await server_module.og_memory_insert(content="User likes tea", meta={"category": "drink"})

@pytest.mark.asyncio
async def test_create_vector_index_executes_validated_statements(mock_db_connection):
    _, mock_cursor = mock_db_connection

    result = await create_vector_index(
        table_name="analytics.items",
        column_name="embedding",
        index_type="HNSW",
        distance_ops="VECTOR_COSINE_OPS",
        options={"M": 16, "ef_construction": 64},
    )

    assert result == "create success!"
    assert mock_cursor.execute.call_args_list == [
        call('ALTER TABLE "analytics"."items" SET (parallel_workers = 32)'),
        call('DROP INDEX IF EXISTS "analytics"."items_embedding_vector_idx"'),
        call(
            'CREATE INDEX "items_embedding_vector_idx" ON "analytics"."items" '
            'USING hnsw ("embedding" vector_cosine_ops) '
            'WITH (m = 16, ef_construction = 64)'
        ),
    ]


@pytest.mark.asyncio
async def test_create_vector_index_executes_diskann_statements(mock_db_connection):
    _, mock_cursor = mock_db_connection

    result = await create_vector_index(
        table_name="analytics.items",
        column_name="embedding",
        index_type="DiskANN",
        distance_ops="VECTOR_COSINE_OPS",
        options={"index_size": 100},
    )

    assert result == "create success!"
    assert mock_cursor.execute.call_args_list == [
        call('ALTER TABLE "analytics"."items" SET (parallel_workers = 32)'),
        call('DROP INDEX IF EXISTS "analytics"."items_embedding_vector_idx"'),
        call(
            'CREATE INDEX "items_embedding_vector_idx" ON "analytics"."items" '
            'USING diskann ("embedding" vector_cosine_ops) '
            'WITH (index_size = 100)'
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("index_size", [15, 1001, "100", True])
async def test_create_vector_index_rejects_invalid_diskann_index_size(mock_db_connection, index_size):
    _, mock_cursor = mock_db_connection

    with pytest.raises(ValueError):
        await create_vector_index(
            table_name="analytics.items",
            column_name="embedding",
            index_type="DiskANN",
            distance_ops="VECTOR_COSINE_OPS",
            options={"index_size": index_size},
        )

    mock_cursor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_create_vector_index_rejects_unsupported_diskann_option(mock_db_connection):
    _, mock_cursor = mock_db_connection

    with pytest.raises(ValueError):
        await create_vector_index(
            table_name="analytics.items",
            column_name="embedding",
            index_type="DiskANN",
            distance_ops="VECTOR_COSINE_OPS",
            options={"lists": 100},
        )

    mock_cursor.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "error_match"),
    [
        ({"table_name": "items; DROP TABLE users"}, "Invalid SQL identifier"),
        ({"column_name": "embedding) WHERE true --"}, "Invalid SQL identifier"),
        ({"options": {"m": "16; DROP TABLE users"}}, "options.m must be an integer"),
        ({"options": {1: 16}}, "Option names must be strings"),
        ({"options": {"M": 16, "m": 32}}, "Duplicate option"),
    ],
)
async def test_create_vector_index_rejects_unsafe_input_without_sql(
    mock_db_connection,
    overrides,
    error_match,
):
    _, mock_cursor = mock_db_connection
    arguments = {
        "table_name": "items",
        "column_name": "embedding",
        "options": {"m": 16},
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match=error_match):
        await create_vector_index(**arguments)

    mock_cursor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_vector_search_parameterizes_structured_filters(mock_db_connection):
    _, mock_cursor = mock_db_connection
    mock_cursor.description = [("id",), ("content",), ("score",)]
    mock_cursor.fetchall.return_value = [(1, "safe", 0.25)]
    malicious_value = "tenant' OR 1=1 --"

    result = await vector_search(
        table_name="public.documents",
        vector_data="[0.1, 0.2]",
        vec_column_name="embedding",
        distance_func="cosine",
        other_where_clause=[
            {"column": "tenant_id", "operator": "=", "value": malicious_value},
            {"column": "status", "operator": "IN", "value": ["active", "pending"]},
            {"column": "deleted_at", "operator": "IS NULL"},
        ],
        topk=7,
        output_column_name=["id", "content"],
    )

    expected_sql = (
        'SELECT "id", "content", "embedding" <=> %s::vector AS score '
        'FROM "public"."documents" WHERE "tenant_id" = %s '
        'AND "status" IN (%s, %s) AND "deleted_at" IS NULL '
        'ORDER BY score LIMIT %s'
    )
    assert result == [{"id": 1, "content": "safe", "score": 0.25}]
    assert mock_cursor.execute.call_args_list == [
        call("SET enable_seqscan = off"),
        call("SET enable_indexscan = on"),
        call(
            expected_sql,
            ["[0.1, 0.2]", malicious_value, "active", "pending", 7],
        ),
    ]
    assert malicious_value not in expected_sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("param_name", "param_value"),
    [
        ("hnsw_ef_search", 0),
        ("hnsw_ef_search", 1001),
        ("hnsw_ef_search", "40"),
        ("hnsw_ef_search", True),
        ("nprobes", 0),
        ("nprobes", 32769),
        ("nprobes", "8"),
        ("nprobes", False),
    ],
)
async def test_multi_vector_search_rejects_invalid_integer_search_params_before_extension(
    param_name,
    param_value,
):
    init_pool = MagicMock()
    execute_multi_search = MagicMock()
    close_conn_pool = MagicMock()

    with patch.object(psycopg2.extras, "init_conn_pool", init_pool, create=True), patch.object(
        psycopg2.extras,
        "execute_multi_search",
        execute_multi_search,
        create=True,
    ), patch.object(
        psycopg2.extras,
        "close_conn_pool",
        close_conn_pool,
        create=True,
    ):
        with pytest.raises(ValueError):
            await multi_vector_search(
                table_name="documents",
                vectors=[[0.1, 0.2]],
                vector_field="embedding",
                search_params={param_name: param_value},
            )

    init_pool.assert_not_called()
    execute_multi_search.assert_not_called()
    close_conn_pool.assert_not_called()


def test_normalize_search_params_maps_compatible_names_to_database_gucs():
    search_params = {
        "enable_seqscan": False,
        "enable_indexscan": "ON",
        "hnsw_ef_search": 40,
        "nprobes": 8,
    }

    normalized_params = server_module._normalize_search_params(search_params)

    assert normalized_params == {
        "enable_seqscan": "off",
        "enable_indexscan": "on",
        "hnsw_ef_search": 40,
        "ivfflat_probes": 8,
    }
    assert "nprobes" not in normalized_params


def test_normalize_search_params_rejects_database_guc_name_from_caller():
    with pytest.raises(
        ValueError,
        match=r"^Unsupported search_params: ivfflat_probes$",
    ):
        server_module._normalize_search_params({"ivfflat_probes": 8})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"other_where_clause": ["tenant_id = 'unsafe'"]},
        {"table_name": "documents; DROP TABLE users"},
        {"vec_column_name": "embedding) OR true --"},
        {"output_column_name": ["id, secret"]},
    ],
)
async def test_vector_search_rejects_raw_filters_and_unsafe_identifiers(
    mock_db_connection,
    arguments,
):
    _, mock_cursor = mock_db_connection
    call_arguments = {
        "table_name": "documents",
        "vector_data": "[0.1, 0.2]",
        "vec_column_name": "embedding",
    }
    call_arguments.update(arguments)

    with pytest.raises(ValueError):
        await vector_search(**call_arguments)

    mock_cursor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_hybrid_search_rejects_structured_filters_before_search():
    fulltext_mock = AsyncMock()
    vector_mock = AsyncMock()

    with patch("openGauss_mcp_server.server.fulltext_search", fulltext_mock), patch(
        "openGauss_mcp_server.server.vector_search",
        vector_mock,
    ):
        with pytest.raises(ValueError, match="Hybrid structured filters are not supported"):
            await hybrid_search(
                table_name="documents",
                full_text_search_column_name=["content"],
                keyword="safe",
                vector_data="[0.1, 0.2]",
                other_where_clause=[
                    {"column": "tenant_id", "operator": "=", "value": "tenant-a"}
                ],
            )

    fulltext_mock.assert_not_awaited()
    vector_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_vector_search_uses_quoted_template_and_closes_pool():
    connection_pool = object()
    init_pool = MagicMock(return_value=connection_pool)
    execute_search = MagicMock(return_value=[[{"id": 1}]])
    close_pool = MagicMock()

    with patch.object(psycopg2.extras, "init_conn_pool", init_pool, create=True), patch.object(
        psycopg2.extras,
        "execute_multi_search",
        execute_search,
        create=True,
    ), patch.object(
        psycopg2.extras,
        "close_conn_pool",
        close_pool,
        create=True,
    ):
        result = await multi_vector_search(
            table_name="public.documents",
            vectors=[[0.1, 0.2], [0.3, 0.4]],
            vector_field="embedding",
            limit=3,
            output_fields=["id", "content"],
            metric_type="cosine",
            search_params={"hnsw_ef_search": 40, "nprobes": 8},
            parallel_workers=4,
        )

    expected_params = {
        "enable_seqscan": "off",
        "enable_indexscan": "on",
        "hnsw_ef_search": 40,
        "ivfflat_probes": 8,
    }
    expected_sql = (
        'SELECT "id", "content", "embedding" <=> %s::vector AS score '
        'FROM "public"."documents" ORDER BY score LIMIT 3'
    )
    assert result == {"result": [[{"id": 1}]]}
    init_pool.assert_called_once_with(init_pool.call_args.args[0], 4, expected_params)
    execute_search.assert_called_once_with(
        init_pool.call_args.args[0],
        connection_pool,
        expected_sql,
        [("[0.1, 0.2]",), ("[0.3, 0.4]",)],
        expected_params,
        4,
    )
    close_pool.assert_called_once_with(connection_pool)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"filter_expr": "tenant_id = 'unsafe'"},
        {"table_name": "documents; DROP TABLE users"},
        {"vector_field": "embedding) OR true --"},
        {"output_fields": ["id, secret"]},
        {"search_params": {"work_mem": "1GB"}},
        {"search_params": {"enable_seqscan": "unsafe"}},
    ],
)
async def test_multi_vector_search_rejects_unsafe_inputs(arguments):
    call_arguments = {
        "table_name": "documents",
        "vectors": [[0.1, 0.2]],
        "vector_field": "embedding",
    }
    call_arguments.update(arguments)

    with pytest.raises(ValueError):
        await multi_vector_search(**call_arguments)


@pytest.mark.asyncio
async def test_multi_vector_search_closes_pool_after_execution_error():
    connection_pool = object()
    init_pool = MagicMock(return_value=connection_pool)
    execute_search = MagicMock(side_effect=RuntimeError("execution secret"))
    close_pool = MagicMock(side_effect=RuntimeError("close secret"))

    with patch.object(psycopg2.extras, "init_conn_pool", init_pool, create=True), patch.object(
        psycopg2.extras,
        "execute_multi_search",
        execute_search,
        create=True,
    ), patch.object(
        psycopg2.extras,
        "close_conn_pool",
        close_pool,
        create=True,
    ):
        with pytest.raises(ValueError, match="failed with RuntimeError") as exc_info:
            await multi_vector_search(
                table_name="documents",
                vectors=[[0.1, 0.2]],
                vector_field="embedding",
            )

    assert str(exc_info.value) == "Multi-vector search failed with RuntimeError"
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "execution secret"
    close_pool.assert_called_once_with(connection_pool)


@pytest.mark.asyncio
async def test_call_tool_info_log_does_not_include_arguments(caplog, mock_db_connection):
    _, mock_cursor = mock_db_connection
    sql_secret = "SELECT 'super-secret-value'"
    mock_cursor.description = [("value",)]
    mock_cursor.fetchall.return_value = [("ok",)]

    with caplog.at_level(logging.INFO, logger="openGauss_mcp_server"):
        await call_tool("execute_sql", {"query": sql_secret, "token": "argument-secret"})

    assert "Calling tool: execute_sql" in caplog.text
    assert sql_secret not in caplog.text
    assert "argument-secret" not in caplog.text


@pytest.mark.asyncio
async def test_call_tool_database_error_log_does_not_include_query_or_error(
    caplog,
    mock_db_connection,
):
    _, mock_cursor = mock_db_connection
    sql_secret = "SELECT 'query-secret'"
    mock_cursor.execute.side_effect = psycopg2.Error("database-error-secret")

    with caplog.at_level(logging.ERROR, logger="openGauss_mcp_server"):
        result = await call_tool("execute_sql", {"query": sql_secret})

    assert result[0].text == "Error executing query"
    assert "SQL execution failed" in caplog.text
    assert "error_type=Error" in caplog.text
    assert sql_secret not in caplog.text
    assert "database-error-secret" not in caplog.text


@pytest.mark.asyncio
async def test_main_logs_database_configuration_without_values(caplog):
    database_config = {
        "host": "secret-host",
        "port": 5432,
        "user": "secret-user",
        "password": "secret-password",
        "dbname": "secret-database",
    }

    with patch("openGauss_mcp_server.server.get_db_config", return_value=database_config), patch.object(
        app,
        "run_stdio_async",
        AsyncMock(),
    ) as run_stdio, patch.object(sys, "argv", ["openGauss_mcp_server"]):
        with caplog.at_level(logging.INFO, logger="openGauss_mcp_server"):
            await main()

    run_stdio.assert_awaited_once_with()
    assert "Database configuration loaded" in caplog.text
    for secret in database_config.values():
        assert str(secret) not in caplog.text


@pytest.mark.asyncio
async def test_vector_search_log_does_not_include_input_values(caplog, mock_db_connection):
    _, mock_cursor = mock_db_connection
    mock_cursor.description = [("score",)]
    mock_cursor.fetchall.return_value = [(0.1,)]
    secret_table = "private_documents"
    secret_vector = "[0.123456789, 0.987654321]"
    secret_column = "private_embedding"

    with caplog.at_level(logging.INFO, logger="openGauss_mcp_server"):
        await vector_search(
            table_name=secret_table,
            vector_data=secret_vector,
            vec_column_name=secret_column,
        )

    assert "Calling tool: vector_search" in caplog.text
    assert secret_table not in caplog.text
    assert secret_vector not in caplog.text
    assert secret_column not in caplog.text
