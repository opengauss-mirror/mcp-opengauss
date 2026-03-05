import asyncio
import json
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import psycopg2
from psycopg2 import Error
from urllib import error

from openGauss_mcp_server.server import (
    app, get_db_config, list_resources, read_resource, list_tools,
    call_tool, search_opengauss_document, create_fulltext_index,
    fulltext_search, create_vector_index, vector_search, hybrid_search,
    normalize_vector_scores, OGMemory, og_memory_query, og_memory_insert
)

@pytest.fixture(autouse=True)
def setup_env():
    os.environ["OPENGAUSS_USER"] = "test_user"
    os.environ["OPENGAUSS_PASSWORD"] = "test_pwd"
    os.environ["OPENGAUSS_DBNAME"] = "test_db"
    os.environ["OPENGAUSS_PORT"] = "5432"
    yield
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
    assert len(tools) == 4
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

@pytest.mark.asyncio
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
async def test_og_memory(setup_env):
    with patch("openGauss_mcp_server.server.OGMemory") as mock_og_memory:
        mock_og_memory_instance = MagicMock()
        mock_og_memory_instance.get_embedding_context.return_value = [0.1, 0.2]
        mock_og_memory.return_value = mock_og_memory_instance

        with patch("openGauss_mcp_server.server.vector_search") as mock_vector_search:
            mock_vector_search.return_value = [
                {"mem_id": 1, "content": "User likes coffee"}
            ]
            result = await og_memory_query(query="preference", topk=2)
            assert result[0]["mem_id"] == 1
            assert result[0]["content"] == "User likes coffee"

        with patch("openGauss_mcp_server.server.connect") as mock_connect:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_connect.return_value.__enter__.return_value = mock_conn
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            await og_memory_insert(content="User likes tea", meta={"category": "drink"})