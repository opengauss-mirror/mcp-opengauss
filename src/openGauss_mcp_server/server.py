import argparse
import asyncio
import getpass
import json
import logging
import os
import psycopg2
import re
import ssl
from bs4 import BeautifulSoup
from urllib import request, error
from psycopg2 import Error, sql, connect
from typing import Optional, List, Any
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from mcp.types import Resource, Tool, TextContent
from pydantic import AnyUrl

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("openGauss_mcp_server")

SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VECTOR_DISTANCE_OPERATORS = {
    "l2": "<->",
    "cosine": "<=>",
    "ip": "<#>",
}
VECTOR_DISTANCE_OP_CLASSES = {
    "vector_cosine_ops",
    "vector_ip_ops",
    "vector_l1_ops",
    "vector_l2_ops",
}
VECTOR_INDEX_OPTION_RULES = {
    "hnsw": {
        "m": (2, 100),
        "ef_construction": (4, 1000),
    },
    "ivfflat": {
        "lists": (1, 32768),
    },
    "diskann": {
        "index_size": (16, 1000),
    },
}
VECTOR_SEARCH_INTEGER_PARAMETER_RULES = {
    "hnsw_ef_search": (1, 1000),
    "nprobes": (1, 32768),
}
VECTOR_SEARCH_PARAMETER_GUC_NAMES = {
    "hnsw_ef_search": "hnsw_ef_search",
    "nprobes": "ivfflat_probes",
}
VECTOR_FILTER_OPERATORS = {
    "=",
    "!=",
    "<>",
    "<",
    "<=",
    ">",
    ">=",
    "LIKE",
    "ILIKE",
    "IN",
    "IS NULL",
    "IS NOT NULL",
}
VECTOR_FILTER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "column": {"type": "string"},
        "operator": {"type": "string"},
        "value": {},
    },
    "required": ["column", "operator"],
    "additionalProperties": False,
}


def _quote_sql_identifier(identifier: str, *, allow_qualified: bool = False) -> str:
    """Validate and quote an ASCII SQL identifier."""
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("SQL identifier must be a non-empty string")

    identifier_parts = identifier.split(".") if allow_qualified else [identifier]
    if not allow_qualified and "." in identifier:
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")

    if not all(SQL_IDENTIFIER_PATTERN.fullmatch(part) for part in identifier_parts):
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")

    return ".".join(f'"{part}"' for part in identifier_parts)


def _split_table_identifier(table_name: str) -> tuple[Optional[str], str]:
    """Return (schema, table) for a simple or schema-qualified table name."""
    _quote_sql_identifier(table_name, allow_qualified=True)
    if not isinstance(table_name, str) or not table_name:
        raise ValueError("table_name must be a non-empty string")
    parts = table_name.split(".")
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError(f"Invalid SQL identifier: {table_name!r}")


def _discover_unique_key_columns(table_name: str) -> list[str]:
    """Find a non-null primary key or unique constraint for a table.

    Primary keys are preferred.  A unique constraint is accepted only when all
    of its columns are declared NOT NULL, so NULL values cannot produce an
    ambiguous merge key.
    """
    schema_name, relation_name = _split_table_identifier(table_name)
    query = """
        SELECT tc.constraint_type, tc.constraint_name,
               kcu.ordinal_position, kcu.column_name, c.is_nullable
          FROM information_schema.table_constraints AS tc
          JOIN information_schema.key_column_usage AS kcu
            ON tc.constraint_schema = kcu.constraint_schema
           AND tc.constraint_name = kcu.constraint_name
           AND tc.table_name = kcu.table_name
          JOIN information_schema.columns AS c
            ON c.table_schema = kcu.table_schema
           AND c.table_name = kcu.table_name
           AND c.column_name = kcu.column_name
         WHERE tc.table_schema = COALESCE(%s, current_schema())
           AND tc.table_name = %s
           AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
         ORDER BY CASE WHEN tc.constraint_type = 'PRIMARY KEY' THEN 0 ELSE 1 END,
                  tc.constraint_name, kcu.ordinal_position
    """
    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, (schema_name, relation_name))
                rows = cursor.fetchall()
    except Error as error:
        logger.error(
            "Could not discover a stable key: error_type=%s", type(error).__name__
        )
        raise RuntimeError(
            f"Could not inspect table {table_name!r} for a primary or unique key"
        ) from error

    candidates: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for constraint_type, constraint_name, ordinal, column_name, is_nullable in rows:
        candidate = (constraint_type, constraint_name)
        candidates.setdefault(candidate, []).append(
            (ordinal, column_name, is_nullable)
        )
    for (constraint_type, _), columns in candidates.items():
        if all(is_nullable == "NO" for _, _, is_nullable in columns):
            return [column_name for _, column_name, _ in sorted(columns)]
    raise ValueError(
        f"Table {table_name!r} has no non-null primary key or unique key; "
        "specify unique_key_columns explicitly or add a suitable constraint"
    )


def _normalize_unique_key_columns(
    unique_key_columns: Optional[list[str]],
) -> Optional[list[str]]:
    if unique_key_columns is None:
        return None
    if not isinstance(unique_key_columns, list) or not unique_key_columns:
        raise ValueError("unique_key_columns must be a non-empty list when provided")
    normalized: list[str] = []
    seen: set[str] = set()
    for column_name in unique_key_columns:
        _quote_sql_identifier(column_name)
        normalized_name = column_name.lower()
        if normalized_name in seen:
            raise ValueError(f"Duplicate unique key column: {column_name!r}")
        seen.add(normalized_name)
        normalized.append(column_name)
    return normalized


def _validate_integer_range(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_vector_index_options(index_type: str, options: Optional[dict]) -> dict[str, int]:
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ValueError("options must be an object")

    normalized_options: dict[str, Any] = {}
    for option_name, option_value in options.items():
        if not isinstance(option_name, str):
            raise ValueError("Option names must be strings")
        normalized_option_name = option_name.lower()
        if normalized_option_name in normalized_options:
            raise ValueError(
                f"Duplicate option after case normalization: {normalized_option_name}"
            )
        normalized_options[normalized_option_name] = option_value

    option_rules = VECTOR_INDEX_OPTION_RULES[index_type]
    unsupported_options = set(normalized_options) - set(option_rules)
    if unsupported_options:
        unsupported_names = ", ".join(sorted(unsupported_options))
        raise ValueError(f"Unsupported options for {index_type}: {unsupported_names}")

    validated_options = {}
    for option_name, option_value in normalized_options.items():
        minimum, maximum = option_rules[option_name]
        validated_options[option_name] = _validate_integer_range(
            option_value,
            f"options.{option_name}",
            minimum,
            maximum,
        )
    return validated_options


def _build_vector_filter_clause(filters: Optional[list[dict[str, Any]]]) -> tuple[str, list[Any]]:
    if filters is None:
        return "", []
    if not isinstance(filters, list):
        raise ValueError("other_where_clause must be a list of structured filters")

    clauses = []
    parameters: list[Any] = []
    for filter_item in filters:
        if not isinstance(filter_item, dict):
            raise ValueError("Raw SQL filters are no longer supported")
        filter_keys = set(filter_item)
        required_keys = {"column", "operator"}
        allowed_keys = required_keys | {"value"}
        if not required_keys.issubset(filter_keys) or not filter_keys <= allowed_keys:
            raise ValueError("Each filter must contain column and operator, with optional value")

        quoted_column = _quote_sql_identifier(filter_item["column"])
        operator_value = filter_item["operator"]
        if not isinstance(operator_value, str):
            raise ValueError("Filter operator must be a string")
        normalized_operator = " ".join(operator_value.upper().split())
        if normalized_operator not in VECTOR_FILTER_OPERATORS:
            raise ValueError(f"Unsupported filter operator: {operator_value!r}")

        if normalized_operator in {"IS NULL", "IS NOT NULL"}:
            filter_value = filter_item.get("value")
            if filter_value is not None:
                raise ValueError(f"{normalized_operator} filter value must be null")
            clauses.append(f"{quoted_column} {normalized_operator}")
            continue

        if "value" not in filter_item:
            raise ValueError(f"{normalized_operator} filter requires a value")
        filter_value = filter_item["value"]

        if normalized_operator == "IN":
            if not isinstance(filter_value, (list, tuple)) or not filter_value:
                raise ValueError("IN filter value must be a non-empty list")
            placeholders = ", ".join("%s" for _ in filter_value)
            clauses.append(f"{quoted_column} IN ({placeholders})")
            parameters.extend(filter_value)
            continue

        clauses.append(f"{quoted_column} {normalized_operator} %s")
        parameters.append(filter_value)

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), parameters


def _normalize_search_params(search_params: Optional[dict[str, Any]]) -> dict[str, Any]:
    default_params = {"enable_seqscan": "off", "enable_indexscan": "on"}
    if search_params is None:
        return default_params
    if not isinstance(search_params, dict):
        raise ValueError("search_params must be an object")

    allowed_keys = set(default_params) | set(VECTOR_SEARCH_INTEGER_PARAMETER_RULES)
    unsupported_keys = set(search_params) - allowed_keys
    if unsupported_keys:
        unsupported_names = ", ".join(sorted(map(str, unsupported_keys)))
        raise ValueError(f"Unsupported search_params: {unsupported_names}")

    normalized_params: dict[str, Any] = default_params.copy()
    for parameter_name, parameter_value in search_params.items():
        if parameter_name in VECTOR_SEARCH_INTEGER_PARAMETER_RULES:
            minimum, maximum = VECTOR_SEARCH_INTEGER_PARAMETER_RULES[parameter_name]
            guc_name = VECTOR_SEARCH_PARAMETER_GUC_NAMES[parameter_name]
            normalized_params[guc_name] = _validate_integer_range(
                parameter_value,
                f"search_params.{parameter_name}",
                minimum,
                maximum,
            )
            continue
        if isinstance(parameter_value, bool):
            normalized_params[parameter_name] = "on" if parameter_value else "off"
            continue
        if isinstance(parameter_value, str) and parameter_value.lower() in {"on", "off"}:
            normalized_params[parameter_name] = parameter_value.lower()
            continue
        raise ValueError(f"search_params.{parameter_name} must be on, off, or a boolean")
    return normalized_params

class SecureCredentialCache:
    _instance = None
    _db_password: Optional[str] = None
    _ssl_keyfile_password: Optional[str] = None
    _initialized: bool = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def get_db_password(self) -> str:
        if self._db_password is None:
            env_password = os.getenv("OPENGAUSS_PASSWORD")
            if env_password:
                self._db_password = env_password
                logger.info("Database password loaded from environment variable")
            else:
                self._db_password = getpass.getpass("Enter database password: ")
                logger.info("Database password obtained from interactive input")
        return self._db_password
    
    def get_ssl_keyfile_password(self) -> Optional[str]:
        if self._ssl_keyfile_password is None:
            env_ssl_password = os.getenv("SSL_KEYFILE_PASSWORD")
            if env_ssl_password:
                self._ssl_keyfile_password = env_ssl_password
                logger.info("SSL keyfile password loaded from environment variable")
            else:
                enable_https = os.getenv("ENABLE_HTTPS", "false").lower() in ("true", "1", "yes", "on")
                ssl_keyfile = os.getenv("SSL_KEYFILE")
                ssl_certfile = os.getenv("SSL_CERTFILE")
                if enable_https and ssl_keyfile and ssl_certfile:
                    self._ssl_keyfile_password = getpass.getpass("Enter SSL private key password (press Enter if none): ")
                    if self._ssl_keyfile_password == "":
                        self._ssl_keyfile_password = None
                    logger.info("SSL keyfile password obtained from interactive input")
        return self._ssl_keyfile_password
    
    def clear_cache(self):
        self._db_password = None
        self._ssl_keyfile_password = None
        self._initialized = False
        logger.info("Credential cache cleared")

credential_cache = SecureCredentialCache()

def get_db_config():
    """Get database configuration from environment variables."""
    config = {
        "host": os.getenv("OPENGAUSS_HOST", "localhost"),
        "port": int(os.getenv("OPENGAUSS_PORT", "5432")), 
        "user": os.getenv("OPENGAUSS_USER"),
        "password": credential_cache.get_db_password(),
        "dbname": os.getenv("OPENGAUSS_DBNAME"),
    }
    if not all([config["user"], config["dbname"]]):
        raise ValueError("Missing required database configuration (OPENGAUSS_USER, OPENGAUSS_DBNAME)")
    
    return config

# Initialize server
app = FastMCP(
    name="openGauss_mcp_server",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"]
    )
)

@app.tool()
async def list_resources() -> list[Resource]:
    """List openGauss tables as resources."""
    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                tables = cursor.fetchall()
                logger.info(f"Found tables: {tables}")

                resources = []
                for table in tables:
                    resources.append(
                        Resource(
                            uri=f"opengauss://{table[0]}/data",
                            name=f"Table: {table[0]}",
                            mimeType="text/plain",
                            description=f"Data in table: {table[0]}"
                        )
                    )
                return resources
    except Error as e:
        logger.error(f"Failed to list resources: {str(e)}")
        return []

@app.tool()
async def read_resource(uri: AnyUrl) -> str:
    """Read table contents."""
    uri_str = str(uri)
    logger.info(f"Reading resource: {uri_str}")
    
    if not uri_str.lower().startswith("opengauss://"):
        raise ValueError(f"Invalid URI scheme: {uri_str}")
        
    parts = uri_str[12:].split('/')
    table = _quote_sql_identifier(parts[0], allow_qualified=True)
    config = get_db_config()
    
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT * FROM {table} LIMIT 100")
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                result = [",".join(map(str, row)) for row in rows]
                return "\n".join([",".join(columns)] + result)
                
    except Error:
        logger.exception("Database error while reading resource")
        raise RuntimeError("Database operation failed") from None

@app.tool()
async def list_tools() -> list[Tool]:
    """List available openGauss tools."""
    logger.info("Listing tools...")
    tools = [
        Tool(
            name="execute_sql",
            description="Execute an SQL query on the openGauss server",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute"
                    }
                },
                "required": ["query"]
                }
            ),
        Tool(
            name="search_opengauss_document",
            description="Search OpenGauss documentation with keywords",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "title": "Keyword",
                        "type": "string"
                    }
                },
                "required": ["keyword"],
                "title": "search_opengauss_documentArguments"
            }
        ),
        Tool(
            name="hybrid_search",
            description="Hybrid search combining BM25 full-text search, vector similarity search, and scalar filtering. Performs weighted fusion of text and vector search results for more accurate and comprehensive search results. Steps: 1) Full-text search with BM25 scoring, 2) Vector similarity search, 3) Weighted fusion and ranking. Stable fusion keys are auto-detected from the table primary/unique key; unique_key_columns can override this. Note: hybrid_score, vector_norm, bm25_norm are computed fields, not database columns.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "title": "Table Name",
                        "type": "string"
                    },
                    "full_text_search_column_name": {
                        "items": {
                            "type": "string"
                        },
                        "title": "Full Text Search Column Name",
                        "type": "array"
                    },
                    "keyword": {
                        "title": "Keyword",
                        "type": "string"
                    },
                    "vector_data": {
                        "title": "Vector Data",
                        "type": "string"
                    },
                    "vec_column_name": {
                        "default": "vector",
                        "title": "Vec Column Name",
                        "type": "string"
                    },
                    "distance_func": {
                        "default": "cosine",
                        "title": "Distance Func",
                        "type": "string"
                    },
                    "other_where_clause": {
                        "anyOf": [
                            {
                                "items": VECTOR_FILTER_INPUT_SCHEMA,
                                "type": "array"
                            },
                            {
                                "type": "null"
                            }
                        ],
                        "default": None,
                        "title": "Other Where Clause",
                        "description": "Structured filters; non-empty filters are currently rejected until the full-text path is secured"
                    },
                    "limit": {
                        "default": 5,
                        "title": "Limit",
                        "type": "integer"
                    },
                    "output_column_name": {
                        "anyOf": [
                            {
                                "items": {
                                    "type": "string"
                                },
                                "type": "array"
                            },
                            {
                                "type": "null"
                            }
                        ],
                        "default": None,
                        "title": "Output Column Name"
                    },
                    "unique_key_columns": {
                        "anyOf": [
                            {"items": {"type": "string"}, "minItems": 1, "type": "array"},
                            {"type": "null"}
                        ],
                        "default": None,
                        "title": "Unique Key Columns",
                        "description": "Optional stable key columns for result fusion; auto-detected from the table primary/unique key when omitted"
                    },
                    "text_weight": {
                        "default": 0.4,
                        "title": "Text Weight",
                        "type": "number"
                    },
                    "vector_weight": {
                        "default": 0.6,
                        "title": "Vector Weight",
                        "type": "number"
                    }
                },
                "required": [
                    "table_name",
                    "full_text_search_column_name",
                    "keyword",
                    "vector_data"
                ],
                "title": "hybrid_searchArguments"
            }
        ),
        Tool(
            name="multi_vector_search",
            description="Perform concurrent vector similarity search with multiple query vectors. Optimizes performance by processing multiple vector queries in parallel using database connection pooling. Ideal for batch vector search operations and multi-vector database lookups. Returns combined results from all query vectors with configurable parallelism.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "title": "Table Name",
                        "type": "string"
                    },
                    "vectors": {
                        "items": {
                            "items": {
                                "type": "number"
                            },
                            "type": "array"
                        },
                        "title": "Vectors",
                        "type": "array"
                    },
                    "vector_field": {
                        "title": "Vector Field",
                        "type": "string"
                    },
                    "limit": {
                        "default": 5,
                        "title": "Limit",
                        "type": "integer"
                    },
                    "output_fields": {
                        "anyOf": [
                            {
                                "items": {
                                    "type": "string"
                                },
                                "type": "array"
                            },
                            {
                                "type": "null"
                            }
                        ],
                        "default": None,
                        "title": "Output Fields"
                    },
                    "metric_type": {
                        "default": "cosine",
                        "title": "Metric Type",
                        "type": "string"
                    },
                    "filter_expr": {
                        "type": "null",
                        "default": None,
                        "title": "Filter Expr",
                        "description": "Raw SQL filters are disabled for security"
                    },
                    "search_params": {
                        "anyOf": [
                            {
                                "additionalProperties": True,
                                "type": "object"
                            },
                            {
                                "type": "null"
                            }
                        ],
                        "default": None,
                        "title": "Search Params"
                    },
                    "parallel_workers": {
                        "default": 2,
                        "title": "Parallel Workers",
                        "type": "integer"
                    }
                },
                "required": ["table_name", "vectors", "vector_field"],
                "title": "multi_vector_searchArguments"
            }
        ),
        Tool(
            name="create_vector_index",
            description="Create a vector index on a specified table and column for efficient vector similarity search. Supports HNSW and other index types with customizable distance metrics.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "title": "Table Name",
                        "type": "string",
                        "description": "The name of the table on which to create the vector index"
                    },
                    "column_name": {
                        "title": "Column Name",
                        "type": "string",
                        "description": "The name of the vector column to index"
                    },
                    "index_type": {
                        "title": "Index Type",
                        "type": "string",
                        "default": "hnsw",
                        "description": "The type of vector index to create (e.g., 'hnsw', 'ivfflat', 'diskann')"
                    },
                    "distance_ops": {
                        "title": "Distance Operations",
                        "type": "string",
                        "default": "vector_cosine_ops",
                        "description": "The distance metric operator (e.g., 'vector_cosine_ops', 'vector_l2_ops', 'vector_ip_ops', 'vector_l1_ops')"
                    },
                    "options": {
                        "title": "Index Options",
                        "type": "object",
                        "default": None,
                        "description": "Additional index options as key-value pairs (e.g., {'M': 16, 'ef_construction': 64} for HNSW)",
                        "additionalProperties": True
                    }
                },
                "required": ["table_name", "column_name"],
                "title": "create_vector_indexArguments"
            }
        ),
        Tool(
            name="vector_search",
            description="Perform vector similarity search on an OpenGauss table to find the most similar vectors to a query vector. Supports different distance metrics and filtering conditions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "title": "Table Name",
                        "type": "string",
                        "description": "The name of the table to search"
                    },
                    "vector_data": {
                        "title": "Vector Data",
                        "type": "string",
                        "description": "The query vector data (JSON string format)"
                    },
                    "vec_column_name": {
                        "title": "Vector Column Name",
                        "type": "string",
                        "default": "vector",
                        "description": "The name of the column containing vectors to search"
                    },
                    "distance_func": {
                        "title": "Distance Function",
                        "type": "string",
                        "default": "cosine",
                        "description": "The distance algorithm to use: 'cosine' (cosine distance), 'l2' (Euclidean distance), or 'ip' (inner product)"
                    },
                    "other_where_clause": {
                        "title": "Other Where Clause",
                        "type": "array",
                        "items": VECTOR_FILTER_INPUT_SCHEMA,
                        "default": None,
                        "description": "Structured, parameterized filters for vector search results"
                    },
                    "topk": {
                        "title": "Top K",
                        "type": "integer",
                        "default": 5,
                        "description": "Number of top similar results to return"
                    },
                    "output_column_name": {
                        "title": "Output Column Name",
                        "type": "array",
                        "items": {
                            "type": "string"
                        },
                        "default": None,
                        "description": "List of column names to include in the output. If None, returns all columns"
                    }
                },
                "required": ["table_name", "vector_data"],
                "title": "vector_searchArguments"
            }
        )
    ]
    
    return tools


def handle_meta_command(cursor, query: str, config: dict) -> list[TextContent]:
    """Handle OpenGauss meta-commands (e.g., \d, \dt)."""
    command = query.strip()
    
    # Handle \d (list tables)
    if command == "\\d":
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = cursor.fetchall()
        result = ["Tables_in_" + config["dbname"]]  # Header
        result.extend([table[0] for table in tables])
        return [TextContent(type="text", text="\n".join(result))]
    
    # Handle \dt (list tables with details)
    elif command == "\\dt":
        cursor.execute("SELECT tablename, tableowner, tablespace FROM pg_tables WHERE schemaname = 'public'")
        columns = ["Table", "Owner", "Tablespace"]
        rows = cursor.fetchall()
        result = [",".join(columns)]  # Header
        result.extend([",".join(map(str, row)) for row in rows])
        return [TextContent(type="text", text="\n".join(result))]
    
    # Handle \d+ (list tables with extended details)
    elif command == "\\d+":
        cursor.execute("SELECT tablename, tableowner, tablespace, hasindexes, hasrules, hastriggers FROM pg_tables WHERE schemaname = 'public'")
        columns = ["Table", "Owner", "Tablespace", "Has Indexes", "Has Rules", "Has Triggers"]
        rows = cursor.fetchall()
        result = [",".join(columns)]  # Header
        result.extend([",".join(map(str, row)) for row in rows])
        return [TextContent(type="text", text="\n".join(result))]
    
    # Handle \du (list users and roles)
    elif command == "\\du":
        cursor.execute("SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin FROM pg_roles")
        columns = ["Role", "Superuser", "Inherit", "Create Role", "Create DB", "Can Login"]
        rows = cursor.fetchall()
        result = [",".join(columns)]  # Header
        result.extend([",".join(map(str, row)) for row in rows])
        return [TextContent(type="text", text="\n".join(result))]
    
    # Unsupported meta-command
    else:
        return [TextContent(type="text", text=f"Unsupported meta-command: {command}")]

@app.tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute SQL commands."""
    config = get_db_config()
    logger.info("Calling tool: %s", name)
    
    if name != "execute_sql":
        raise ValueError(f"Unknown tool: {name}")
    
    query = arguments.get("query")
    if not query:
        raise ValueError("Query is required")
    
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                
                if query.strip().startswith("\\"):
                    return handle_meta_command(cursor, query, config)
                
                # Execute regular SQL queries
                cursor.execute(query)

                # Regular SELECT queries
                if query.strip().upper().startswith("SELECT"):
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    result = [",".join(map(str, row)) for row in rows]
                    return [TextContent(type="text", text="\n".join([",".join(columns)] + result))]
                
                # Explain SQL
                elif query.strip().upper().startswith("EXPLAIN"):
                    rows = cursor.fetchall()
                    plan_lines = [row[0] for row in rows]
                    return [TextContent(type="text", text="\n".join(plan_lines))]

                # Non-SELECT queries
                else:
                    conn.commit()
                    return [TextContent(type="text", text=f"Query executed successfully. Rows affected: {cursor.rowcount}")]
                
    except Error as error:
        logger.error(
            "SQL execution failed: error_type=%s pgcode=%s",
            type(error).__name__,
            getattr(error, "pgcode", None),
        )
        return [TextContent(type="text", text="Error executing query")]

@app.tool()
async def search_opengauss_document(keyword: str):
    """
    This tool is designed to provide context-specific information about OpenGauss to a large language model (LLM) to enhance the accuracy and relevance of its responses.
    The LLM should automatically extracts relevant search keywords from user queries or LLM's answer for the tool parameter "keyword".
    The main functions of this tool include:
    1.Information Retrieval: The MCP Tool searches through OpenGauss-related documentation using the extracted keywords, locating and extracting the most relevant information.
    2.Context Provision: The retrieved information from OpenGauss documentation is then fed back to the LLM as contextual reference material. This context is not directly shown to the user but is used to refine and inform the LLM’s responses.
    This tool ensures that when the LLM’s internal documentation is insufficient to generate high-quality responses, it dynamically retrieves necessary OpenGauss information, thereby maintaining a high level of response accuracy and expertise.
    Keywords can now include both English and Chinese.
    """
    logger.info(f"Calling tool: search_opengauss_document,keyword:{keyword}")
    search_api_url = (
        "https://docs.opengauss.org/api-search/search/sort/docs"
    )
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json;charset=UTF-8",
        "Origin": "https://docs.opengauss.org",
        "Referer": "https://docs.opengauss.org",
        "Source": "opengauss"
    }
    query_param = {
        "keyword": keyword,
        "page": 1,
        "pageSize": 5,
        "lang": "zh",
        "version": "latest",
    }

    query_param = json.dumps(query_param).encode("utf-8")
    req = request.Request(search_api_url, data=query_param, headers=headers, method="POST")

    context = ssl.create_default_context()
    try:
        with request.urlopen(req, timeout=5, context=context) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            json_data = json.loads(response_body)

            records = json_data.get("obj", {}).get("records", [])
            result_list = []
            for item in records:
                doc_url = "https://docs.opengauss.org/" + item["path"] + ".html"
                logger.info(f"doc_url:{doc_url}")
                content = get_opengauss_doc_content(doc_url)
                result_list.append(content)
            return json.dumps(result_list, ensure_ascii=False)
    except error.HTTPError as e:
        logger.error(f"HTTP Error: {e.code} - {e.reason}")
        return "No results were found"
    except error.URLError as e:
        logger.error(f"URL Error: {e.reason}")
        return "No results were found"


def get_opengauss_doc_content(doc_url: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://docs.opengauss.org",
    }

    try:
        ascii_url = doc_url.encode('ascii').decode('ascii')
    except UnicodeEncodeError:
        from urllib.parse import quote
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(doc_url)
        encoded_path = quote(parsed.path, safe='')
        encoded_url = urlunparse((
            parsed.scheme,
            parsed.netloc,
            encoded_path,
            parsed.params,
            parsed.query,
            parsed.fragment
        ))
        doc_url = encoded_url

    req = request.Request(doc_url, headers=headers, method="GET")
    context = ssl.create_default_context()
    try:
        with request.urlopen(req, timeout=5, context=context) as response:
            html = response.read().decode("utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")

            for element in soup(["script", "style", "nav", "header", "footer"]):
                element.decompose()

            text = soup.get_text()

            lines = (line.strip() for line in text.splitlines())
            text = "\n".join(line for line in lines if line)
            logger.info(f"text length:{len(text)}")

            if len(text) > 8000:
                text = text[:8000] + "... [content truncated]"

            title = soup.title.string if (soup.title and soup.title.string) else "openGauss 文档"
            
            if not isinstance(title, str):
                title = str(title)
            if not isinstance(doc_url, str):
                doc_url = str(doc_url)
            if not isinstance(text, str):
                text = str(text)
                
            final_result = {
                "title": title,
                "url": doc_url,
                "content": text,
            }
            return final_result
    except error.HTTPError as e:
        logger.error(f"HTTP Error: {e.code} - {e.reason}")
        return {"result": "No results were found"}
    except error.URLError as e:
        logger.error(f"URL Error: {e.reason}")
        return {"result": "No results were found"}

def ensure_bm25_index(cursor, table_name: str, column_name: str):
    """
    Ensure BM25 index exists; create it if missing.
    """
    quoted_table_name = _quote_sql_identifier(table_name, allow_qualified=True)
    quoted_column_name = _quote_sql_identifier(column_name)
    index_name = f"bm25_{table_name.replace('.', '_')}_{column_name}"
    quoted_index_name = _quote_sql_identifier(index_name)

    cursor.execute(
        "SELECT indexname FROM pg_indexes WHERE indexname = %s;",
        (index_name,)
    )
    exists = cursor.fetchone()

    if exists:
        logger.info(f"BM25 index {index_name} already exists")
        return False  

    # create index
    logger.info(f"BM25 index {index_name} not found, creating...")
    cursor.execute(
        f"CREATE INDEX {quoted_index_name} ON {quoted_table_name} "
        f"USING bm25({quoted_column_name});"
    )

    # check
    cursor.execute(
        "SELECT indexname FROM pg_indexes WHERE indexname = %s;",
        (index_name,)
    )
    created = cursor.fetchone()

    if not created:
        raise RuntimeError(f"Failed to create BM25 index {index_name}")

    logger.info(f"BM25 index {index_name} created successfully")
    return True 

@app.tool()
async def create_fulltext_index(table_name: str, column_name: str):
    """
    Create a BM25 full-text index in an OpenGauss table.
    """
    config = get_db_config()

    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                return ensure_bm25_index(cursor, table_name, column_name)

    except Error as e:
        logger.error(f"Database error: {str(e)}")
        raise RuntimeError(f"Database error: {str(e)}")

@app.tool()
async def fulltext_search(
    table_name: str,
    full_text_search_column_name: list[str],
    keyword: str,
    other_where_clause: Optional[list[dict[str, Any]]] = None,
    limit: int = 5,
    output_column_name: Optional[list[str]] = None,
):
    """
    Search for documents using full text search in a OpenGauss table.
    Automatically creates bm25 index if missing.
    """
    main_search_column = full_text_search_column_name[0]

    quoted_table_name = _quote_sql_identifier(table_name, allow_qualified=True)
    quoted_search_column = _quote_sql_identifier(main_search_column)
    if output_column_name:
        select_columns = ", ".join(
            _quote_sql_identifier(column_name) for column_name in output_column_name
        )
    else:
        select_columns = "*"
    where_clause, filter_parameters = _build_vector_filter_clause(other_where_clause)
    query = (
        "set enable_seqscan = off; set enable_indexscan = on;"
        f"SELECT {select_columns}, {quoted_search_column} <&> %s as score "
        f"FROM {quoted_table_name}{where_clause} "
        f"ORDER BY {quoted_search_column} <&> %s desc limit %s"
    )
    params = [keyword, *filter_parameters, keyword, limit]
    config = get_db_config()

    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                ensure_bm25_index(cursor, table_name, main_search_column)
                cursor.execute(query, params)

                columns = [desc[0] for desc in cursor.description]

                rows = cursor.fetchall()
                result = [dict(zip(columns, row)) for row in rows]

                return result

    except Error:
        logger.exception("Full-text search database operation failed")
        return "Error executing query"

@app.tool()
async def create_vector_index(
    table_name: str,
    column_name: str,
    index_type: str = "hnsw",
    distance_ops: str = "vector_cosine_ops",
    options: dict | None = None,
):
    """Create a validated vector index, replacing an existing index with the same name."""
    quoted_table_name = _quote_sql_identifier(table_name, allow_qualified=True)
    quoted_column_name = _quote_sql_identifier(column_name)

    normalized_index_type = index_type.lower() if isinstance(index_type, str) else ""
    if normalized_index_type not in VECTOR_INDEX_OPTION_RULES:
        allowed_index_types = ", ".join(sorted(VECTOR_INDEX_OPTION_RULES))
        raise ValueError(f"Unsupported index_type. Allowed values are: {allowed_index_types}")

    normalized_distance_ops = distance_ops.lower() if isinstance(distance_ops, str) else ""
    if normalized_distance_ops not in VECTOR_DISTANCE_OP_CLASSES:
        allowed_distance_ops = ", ".join(sorted(VECTOR_DISTANCE_OP_CLASSES))
        raise ValueError(f"Unsupported distance_ops. Allowed values are: {allowed_distance_ops}")

    validated_options = _validate_vector_index_options(normalized_index_type, options)
    table_name_parts = table_name.split(".")
    table_name_for_index = table_name_parts[-1]
    index_name = f"{table_name_for_index}_{column_name}_vector_idx"
    qualified_index_name = (
        f"{table_name_parts[0]}.{index_name}"
        if len(table_name_parts) == 2
        else index_name
    )
    quoted_index_name = _quote_sql_identifier(index_name)
    quoted_drop_index_name = _quote_sql_identifier(
        qualified_index_name,
        allow_qualified=True,
    )

    with_clause = ""
    if validated_options:
        option_sql = ", ".join(
            f"{option_name} = {option_value}"
            for option_name, option_value in validated_options.items()
        )
        with_clause = f" WITH ({option_sql})"

    alter_statement = f"ALTER TABLE {quoted_table_name} SET (parallel_workers = 32)"
    drop_statement = f"DROP INDEX IF EXISTS {quoted_drop_index_name}"
    create_statement = (
        f"CREATE INDEX {quoted_index_name} ON {quoted_table_name} "
        f"USING {normalized_index_type} "
        f"({quoted_column_name} {normalized_distance_ops}){with_clause}"
    )

    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(alter_statement)
                cursor.execute(drop_statement)
                cursor.execute(create_statement)
    except Error as error:
        logger.error(
            "Vector index database operation failed: error_type=%s pgcode=%s",
            type(error).__name__,
            getattr(error, "pgcode", None),
        )
        raise RuntimeError("Vector index database operation failed") from error

    return "create success!"

@app.tool()
async def vector_search(
    table_name: str,
    vector_data: str,
    vec_column_name: str = "vector",
    distance_func: Optional[str] = "cosine",
    other_where_clause: Optional[list[dict[str, Any]]] = None,
    topk: int = 5,
    output_column_name: Optional[list[str]] = None,
):
    """Perform a parameterized vector similarity search."""
    logger.info("Calling tool: vector_search")

    quoted_table_name = _quote_sql_identifier(table_name, allow_qualified=True)
    quoted_vector_column = _quote_sql_identifier(vec_column_name)
    if output_column_name is None:
        select_columns = "*"
    elif not isinstance(output_column_name, list) or not output_column_name:
        raise ValueError("output_column_name must be a non-empty list when provided")
    else:
        select_columns = ", ".join(
            _quote_sql_identifier(column_name) for column_name in output_column_name
        )

    normalized_distance_func = distance_func.lower() if isinstance(distance_func, str) else ""
    if normalized_distance_func not in VECTOR_DISTANCE_OPERATORS:
        allowed_values = ", ".join(sorted(VECTOR_DISTANCE_OPERATORS))
        raise ValueError(
            f"Unsupported distance_func {distance_func!r}. Allowed values are: {allowed_values}"
        )
    distance_operator = VECTOR_DISTANCE_OPERATORS[normalized_distance_func]
    validated_topk = _validate_integer_range(topk, "topk", 1, 10000)
    where_clause, filter_parameters = _build_vector_filter_clause(other_where_clause)

    select_statement = (
        f"SELECT {select_columns}, {quoted_vector_column} {distance_operator} %s::vector AS score "
        f"FROM {quoted_table_name}{where_clause} ORDER BY score LIMIT %s"
    )
    query_parameters = [vector_data, *filter_parameters, validated_topk]

    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET enable_seqscan = off")
                cursor.execute("SET enable_indexscan = on")
                cursor.execute(select_statement, query_parameters)
                rows = cursor.fetchall()
                column_names = [description[0] for description in cursor.description]
                result = [dict(zip(column_names, row)) for row in rows]
    except Error as error:
        logger.error(
            "Vector search database operation failed: error_type=%s pgcode=%s",
            type(error).__name__,
            getattr(error, "pgcode", None),
        )
        raise RuntimeError("Vector search database operation failed") from error

    return result

def normalize_vector_scores(distances, distance_type="cosine"):
    """
    Normalize vector distances into similarity scores (0~1).
    distances: list of raw distances from opengauss
    distance_type: "l2", "cosine", "ip"
    """
    if not distances:
        return []

    if distance_type == "cosine":
        # cosine distance = 1 - cos
        sims = [(2 - d) / 2 for d in distances]

    elif distance_type == "l2":
        # L2 distance = sqrt(sum((xi-yi)^2))
        max_s, min_s = max(distances), min(distances)   #最大值归一化
        if max_s == min_s: 
            sims = [1.0 for _ in distances] 
        else:
            norms = [(s - min_s) / (max_s - min_s) for s in distances]
            sims = [1.0 - nd for nd in norms]

    elif distance_type == "ip":
        #IP distance = - dot(x, y)
        true_inner_products = [-d for d in distances]
        max_ip, min_ip = max(true_inner_products), min(true_inner_products)
        if max_ip == min_ip:
            sims = [1.0 for _ in true_inner_products]
        else:
            norms_ip = [(ip - min_ip) / (max_ip - min_ip) for ip in true_inner_products]
            sims = norms_ip

    else:
        raise ValueError(f"Unsupported distance type: {distance_type}")
    return sims

@app.tool()
async def hybrid_search(
    table_name: str,
    full_text_search_column_name: list[str],
    keyword: str,
    vector_data: str,
    vec_column_name: str = "vector",
    distance_func: str = "cosine",
    other_where_clause: Optional[list[dict[str, Any]]] = None,
    limit: int = 5,
    output_column_name: Optional[list[str]] = None,
    text_weight: float = 0.4,
    vector_weight: float = 0.6,
    unique_key_columns: Optional[list[str]] = None,
):
    """
    Hybrid search: Fusion query combining BM25 full-text search, 
    vector search, and scalar retrieval.
    Steps:
    1. Fulltext search → normalize BM25 score
    2. Vector search → normalize vector score
    3. Weighted fusion → sort → top limit

    ⚠️ IMPORTANT NOTES:
    - `hybrid_score`, `vector_norm`, `bm25_norm`, and `score` are **computed fields**, 
      NOT actual database table columns.
    - These fields only exist in the returned results and are NOT persisted to the database.
    - They are generated during query execution for ranking and scoring purposes.
    
    Field Definitions:
    - `score`: Raw relevance score returned by individual search algorithms (BM25/vector)
    - `bm25_norm`: Normalized BM25 score (0-1 range)
    - `vector_norm`: Normalized vector similarity score (0-1 range)
    - `hybrid_score`: Final fusion score = bm25_norm * text_weight + vector_norm * vector_weight
    
    Fusion Formula:
    hybrid_score = (bm25_norm × text_weight) + (vector_norm × vector_weight)

    `unique_key_columns` may be supplied for non-standard keys or composite
    keys. When omitted, the table primary key or a non-null unique constraint
    is discovered automatically.
    """

    if other_where_clause:
        raise ValueError(
            "Hybrid structured filters are not supported until the full-text path is secured"
        )

    # The two component searches run independently and must be joined on a
    # stable database key.  Prefer an explicitly supplied key; otherwise
    # discover a non-null primary/unique key from the table metadata.
    if output_column_name is None:
        search_output_columns = None  # SELECT * includes the discovered key.
    elif not isinstance(output_column_name, list) or not output_column_name:
        raise ValueError("output_column_name must be a non-empty list when provided")
    else:
        if any(
            not isinstance(column, str) or not column
            for column in output_column_name
        ):
            raise ValueError("output_column_name must contain non-empty strings")
        search_output_columns = list(output_column_name)

    normalized_key_columns = _normalize_unique_key_columns(unique_key_columns)
    if normalized_key_columns is None:
        normalized_key_columns = _discover_unique_key_columns(table_name)
    if search_output_columns is not None:
        existing_columns = {column.lower() for column in search_output_columns}
        for key_column in normalized_key_columns:
            if key_column.lower() not in existing_columns:
                search_output_columns.append(key_column)

    # -----------------------------
    # 1. FULLTEXT SEARCH
    # -----------------------------
    bm25_results = await fulltext_search(
        table_name=table_name,
        full_text_search_column_name=full_text_search_column_name,
        keyword=keyword,
        other_where_clause=None,
        limit=1024,  # 取更多用于融合
        output_column_name=search_output_columns,
    )
    if not isinstance(bm25_results, list):
        raise RuntimeError("Full-text search did not return a result list")

    # 提取 BM25 分数
    bm25_scores = [item["score"] for item in bm25_results] if bm25_results else []

    if bm25_scores:
        max_bm25 = max(bm25_scores)
        if max_bm25 > 0:
            for item in bm25_results:
                item["bm25_norm"] = item["score"] / max_bm25
        else:
            for item in bm25_results:
                item["bm25_norm"] = 0
    else:
        bm25_results = []
    
    # -----------------------------
    # 2. VECTOR SEARCH
    # -----------------------------
    vector_results = await vector_search(
        table_name=table_name,
        vector_data=vector_data,
        vec_column_name=vec_column_name,
        distance_func=distance_func,
        other_where_clause=None,
        topk=1024,
        output_column_name=search_output_columns,
    )
    if not isinstance(vector_results, list):
        raise RuntimeError("Vector search did not return a result list")

    # 向量距离越小越相似 → 转成相似度(距离越小分数越大)
    vector_scores = [item["score"] for item in vector_results]

    vector_norm = normalize_vector_scores(
        vector_scores,
        distance_type=(
            distance_func.lower()
            if isinstance(distance_func, str)
            else distance_func
        )
    )

    for item, norm in zip(vector_results, vector_norm):
        item["vector_norm"] = norm
    
    # -----------------------------
    # 3. MERGE RESULT
    # -----------------------------
    merged = {}

    def result_id(item: dict) -> object:
        values = []
        for key_column in normalized_key_columns:
            value = item.get(key_column)
            if value is None and key_column not in item:
                case_insensitive_key = next(
                    (key for key in item if isinstance(key, str) and key.lower() == key_column.lower()),
                    None,
                )
                value = item.get(case_insensitive_key) if case_insensitive_key else None
            if value is None:
                raise ValueError(
                    "hybrid_search result is missing a non-null unique key value: "
                    + ", ".join(normalized_key_columns)
                )
            values.append(value)
        return values[0] if len(values) == 1 else tuple(values)

    #merge bm25 results
    for item in bm25_results:
        rid = result_id(item)
        merged[rid] = item
        merged[rid]["bm25_norm"] = item.get("bm25_norm", 0)
        merged[rid]["vector_norm"] = 0

    #merge vector results
    for item in vector_results:
        rid = result_id(item)
        if rid not in merged:
            merged[rid] = item
            merged[rid]["bm25_norm"] = 0
        merged[rid]["vector_norm"] = item.get("vector_norm", 0)

    # -----------------------------
    # 4. Calculate the fusion score
    # -----------------------------
    for rid, item in merged.items():
        item["hybrid_score"] = (
            item["bm25_norm"] * text_weight +
            item["vector_norm"] * vector_weight
        )
    
    # -----------------------------
    # 5. Deduplicate and Sort
    # -----------------------------
    deduplicated = {}
    for rid, item in merged.items():
        item.pop("score", None) 
        current_score = item["hybrid_score"]
        
        if rid in deduplicated:
            if current_score > deduplicated[rid]["hybrid_score"]:
                deduplicated[rid] = item
        else:
            deduplicated[rid] = item
    
    final = sorted(
        deduplicated.values(), 
        key=lambda x: x["hybrid_score"], 
        reverse=True
    )
    
    return final[:limit]

@app.tool()
async def multi_vector_search(
    table_name: str,
    vectors: list[list[float]],
    vector_field: str,
    limit: int = 5,
    output_fields: Optional[list[str]] = None,
    metric_type: str = "cosine",
    filter_expr: Optional[str] = None,
    search_params: Optional[dict[str, Any]] = None,
    parallel_workers: int = 2,
):
    """Perform validated vector searches through the openGauss connection pool extension."""
    quoted_table_name = _quote_sql_identifier(table_name, allow_qualified=True)
    quoted_vector_field = _quote_sql_identifier(vector_field)
    if output_fields is None:
        select_columns = "*"
    elif not isinstance(output_fields, list) or not output_fields:
        raise ValueError("output_fields must be a non-empty list when provided")
    else:
        select_columns = ", ".join(
            _quote_sql_identifier(field_name) for field_name in output_fields
        )

    normalized_metric_type = metric_type.lower() if isinstance(metric_type, str) else ""
    if normalized_metric_type not in VECTOR_DISTANCE_OPERATORS:
        allowed_values = ", ".join(sorted(VECTOR_DISTANCE_OPERATORS))
        raise ValueError(
            f"Unsupported metric_type {metric_type!r}. Allowed values are: {allowed_values}"
        )
    metric_operator = VECTOR_DISTANCE_OPERATORS[normalized_metric_type]
    validated_limit = _validate_integer_range(limit, "limit", 1, 10000)
    validated_parallel_workers = _validate_integer_range(
        parallel_workers,
        "parallel_workers",
        1,
        64,
    )
    if filter_expr is not None:
        raise ValueError("Raw SQL filters are no longer supported")
    normalized_search_params = _normalize_search_params(search_params)

    if not isinstance(vectors, list) or not vectors:
        raise ValueError("vectors must be a non-empty list")
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("Each vector must be a non-empty list")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector):
            raise ValueError("Vector values must be numbers")

    try:
        from psycopg2.extras import close_conn_pool, execute_multi_search, init_conn_pool
    except ImportError as error:
        raise ImportError(
            "The current psycopg2 installation does not include the required openGauss "
            "multi-search extensions"
        ) from error

    sql_template = (
        f"SELECT {select_columns}, {quoted_vector_field} {metric_operator} %s::vector AS score "
        f"FROM {quoted_table_name} ORDER BY score LIMIT {validated_limit}"
    )
    argument_list = [(f"[{', '.join(str(value) for value in vector)}]",) for vector in vectors]
    config = get_db_config()
    connection_pool = None

    operation_error: Optional[Exception] = None
    try:
        connection_pool = init_conn_pool(
            config,
            validated_parallel_workers,
            normalized_search_params,
        )
        result = execute_multi_search(
            config,
            connection_pool,
            sql_template,
            argument_list,
            normalized_search_params,
            validated_parallel_workers,
        )
        return {"result": result}
    except Exception as error:
        operation_error = error
        raise ValueError(
            f"Multi-vector search failed with {type(error).__name__}"
        ) from error
    finally:
        if connection_pool is not None:
            try:
                close_conn_pool(connection_pool)
            except Exception as close_error:
                if operation_error is None:
                    raise ValueError(
                        f"Multi-vector search pool close failed with {type(close_error).__name__}"
                    ) from close_error
                logger.error(
                    "Multi-vector search pool close failed after operation error: "
                    "error_type=%s",
                    type(close_error).__name__,
                )

ENABLE_MEMORY = int(os.getenv("ENABLE_MEMORY", 0))
EMBEDDING_MODEL_PROVIDER = os.getenv("EMBEDDING_MODEL_PROVIDER", "huggingface")
LOCAL_MODEL_DIR = os.getenv("LOCAL_MODEL_DIR", "")
REMOTE_MODEL_NAME = os.getenv("REMOTE_MODEL_NAME", "BAAI/bge-small-en-v1.5")

TABLE_NAME_MEMORY = "og_mcp_memory"

if ENABLE_MEMORY:
    class OGMemory:
        def __init__(self):
            self.config = get_db_config()
            self.conn = connect(**self.config)
            self.embedding_dim = len(self.get_embedding_context("test"))
            self.create_memory_table()
        
        def get_embedding_context(self, text:str):
            """
            When loading a model, prioritize the local model directory; 
            if the model does not exist locally, download it online.
            """
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            from langchain_huggingface import HuggingFaceEmbeddings
            
            if EMBEDDING_MODEL_PROVIDER != "huggingface":
                raise ValueError(f"Unsupported embedding model provider: {EMBEDDING_MODEL_PROVIDER}")
            
            if os.path.isdir(LOCAL_MODEL_DIR) and os.listdir(LOCAL_MODEL_DIR):
                logger.info(f"load local model : {LOCAL_MODEL_DIR}")
                model_path = LOCAL_MODEL_DIR
            else:
                logger.info(f"In case the local model is unavailable, the system will automatically download it from the remote server and cache it locally for subsequent use: {REMOTE_MODEL_NAME}")
                model_path = REMOTE_MODEL_NAME

            emb_model = HuggingFaceEmbeddings(
                    model_name=model_path,
                    encode_kwargs={"normalize_embeddings": True},
            )
            return emb_model.embed_query(text)

        def create_memory_table(self):
            with self.conn.cursor() as cur:
                logger.info(f"Check and create table {TABLE_NAME_MEMORY}.")
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {TABLE_NAME_MEMORY} ( 
                        memory_id SERIAL PRIMARY KEY, 
                        content VARCHAR(8000), 
                        embedding VECTOR({self.embedding_dim}), 
                        meta JSON ); 
                """)

                logger.info(f"Check and create hnsw index vidx.")
                cur.execute(f""" 
                    CREATE INDEX  IF NOT EXISTS vidx ON {TABLE_NAME_MEMORY} 
                    USING hnsw (embedding vector_l2_ops) 
                    WITH (m = 16, ef_construction = 200); 
                """)
            self.conn.commit()

    og_memory = OGMemory()
    from psycopg2.extras import Json

    @app.tool()
    async def og_memory_query(query: str, topk: int = 5):
        """
        🧠 COGNITIVE CONTEXT RETRIEVAL ENGINE 🧠 - PERSONALIZED KNOWLEDGE ACCESS SYSTEM
        
        PRIMARY FUNCTION: This module operates as an intelligent assistant that retrieves 
        user-specific contextual information to optimize response personalization and precision.
        
        ===== OPERATIONAL PROTOCOLS =====
        
        [MANDATORY INVOCATION SCENARIOS]
        • Cross-linguistic inquiries about individual preferences and inclinations
        • Pre-storage validation procedures to eliminate information duplication
        • Biographical data disclosure: experiential, professional, geographical contexts
        • Response formulation requiring historical reference and contextual awareness
        • Technical discourse necessitating solution history consultation
        • Personalized recommendation contexts: gastronomic, travel, leisure, gifting
        • Spatiotemporal coordination: scheduling, meteorological, event-based, navigational
        • Professional development guidance: technological tools, educational resources, career pathways
        • Ambiguous requests containing contextual indicators: "appropriate", "suitable", "local", "personalized"
        
        [SYSTEM INTEGRATION PRINCIPLE]
        Multiple relevant knowledge domains require separate invocations for optimal retrieval accuracy.
        
        ===== LINGUISTIC PROCESSING ARCHITECTURE =====
        
        [CROSS-LANGUAGE QUERY MECHANISM]
        • English: "What are my culinary preferences?" → Query: "culinary taste dining preference"
        • Chinese: "我通常去哪里旅行?" → Query: "travel destination frequent location pattern"
        • Spanish: "¿Dónde he trabajado antes?" → Query: "employment history previous workplace"
        
        [SEMANTIC OPTIMIZATION DOCTRINE]
        Primary search vocabulary must be English for vector embedding compatibility, 
        irrespective of original query language composition.
        
        ===== ADVANCED RETRIEVAL METHODOLOGIES =====
        
        [CONTEXTUAL EXPANSION TECHNIQUES]
        1. Temporal Context Integration
           - "I visited Kyoto last spring" → Query: "Kyoto travel spring 2025 seasonal"
           
        2. Professional Network Mapping  
           - "Collaborated with AWS team on cloud migration" → Query: "AWS cloud migration collaboration professional"
           
        3. Skill Proficiency Tracking
           - "Advanced React with TypeScript experience" → Query: "React TypeScript frontend advanced skill"
           
        4. Health & Wellness Monitoring
           - "Meditation practice every morning" → Query: "meditation wellness routine morning habit"
        
        ===== KNOWLEDGE DOMAIN TAXONOMY =====
        
        [DOMAIN-SPECIFIC QUERY TEMPLATES]
        • Physical Activities: "athletic performance exercise regimen fitness milestone"
        • Gastronomic Interests: "culinary exploration flavor profile dietary restriction"
        • Professional Trajectory: "career progression organizational role achievement recognition"
        • Technological Landscape: "software ecosystem development methodology tool proficiency"
        • Personal Ecosystem: "family dynamics lifestyle pattern relationship network"
        • Cultural Consumption: "artistic appreciation media consumption creative expression"
        
        ===== INTELLIGENT KNOWLEDGE INTEGRATION =====
        
        [CONSOLIDATION DECISION MATRIX]
        Scenario A: Domain Congruence
        - New: "Weekly mountain hiking enthusiast"
        - Existing: "Regular gym attendance and protein diet"
        - Analysis: Both belong to wellness domain → Consolidate into comprehensive fitness profile
        
        Scenario B: Domain Divergence  
        - New: "Blockchain development specialization"
        - Existing: "Classical piano performance skill"
        - Analysis: Distinct professional/artistic domains → Maintain separate knowledge entries
        
        Scenario C: Temporal Evolution
        - New: "Recently transitioned to vegan lifestyle"
        - Existing: "Previous omnivorous dietary pattern"
        - Analysis: Same domain with temporal progression → Update with versioning metadata
        
        ===== SYSTEM PARAMETERS =====
        
        [INPUT SPECIFICATIONS]
        • query: Domain-contextual semantic descriptors ("professional development", "leisure preference")
        • topk: Retrieval breadth (recommended 8-12 for comprehensive domain analysis)
        
        [OUTPUT STRUCTURE]
        JSON array containing memory identifiers and content for contextual analysis
        
        [QUALITY ASSURANCE METRIC]
        Complete domain overlap analysis required before any storage modification decisions.
        
        ===== EXTENDED APPLICATIONS =====
        
        [INNOVATIVE USE CASES]
        1. Career Development Advisor
           - Track skill acquisition, certification progress, project experience
           
        2. Health & Wellness Companion
           - Monitor exercise routines, dietary patterns, wellness goals
           
        3. Travel Experience Curator
           - Document destinations, cultural experiences, accommodation preferences
           
        4. Learning Progress Tracker
           - Record educational milestones, knowledge gaps, learning resources
           
        5. Creative Project Archivist
           - Catalog artistic endeavors, creative processes, inspiration sources
        
        [SYSTEM EVOLUTION PATH]
        Future enhancements may include:
        • Emotion-state contextualization
        • Decision pattern recognition  
        • Preference evolution tracking
        • Cross-user knowledge sharing (with privacy safeguards)
        """
        vector_data = str(og_memory.get_embedding_context(query))
        vec_column_name = "embedding"
        distance_func = "l2"
        output_column_name = ["memory_id", "content"]
        res = await vector_search(TABLE_NAME_MEMORY, vector_data, vec_column_name, 
            distance_func, None, topk, output_column_name)
        return res

    @app.tool()
    async def og_memory_insert(content: str, meta: dict):
        """
        🧬 PERSONAL KNOWLEDGE INGESTION ENGINE 🧬 - STRUCTURED INFORMATION ASSIMILATION SYSTEM
        
        PRIMARY FUNCTION: This module serves as an intelligent data ingestion pipeline that 
        systematically processes and organizes user-provided personal information into 
        semantically structured knowledge representations.
        
        ===== OPERATIONAL PROTOCOLS =====
        
        [MANDATORY ACTIVATION CRITERIA]
        • Biographical data disclosure containing domain-specific indicators
        • Personal preference statements with contextual significance
        • Professional experience descriptions requiring archival
        • Lifestyle pattern information with analytical value
        • Achievement documentation for historical tracking
        
        [TRIGGER IDENTIFICATION MATRIX]
        Activation occurs when input contains:
        1. Domain indicator + Factual assertion combination
        2. Temporal context + Personal attribute pairing
        3. Quantitative measurement + Qualitative descriptor
        
        [DOMAIN INDICATOR TAXONOMY]
        • Demographic Profile: chronological age, biological sex, geographical origin, citizenship status
        • Professional Identity: occupational role, organizational affiliation, educational attainment, skill certification
        • Geographical Context: residential location, travel frequency, temporal zone alignment
        • Preference Spectrum: affinity indicators, aversion markers, habitual patterns, aesthetic inclinations
        • Lifestyle Configuration: familial structure, companion animals, daily rituals, belief systems
        • Achievement Portfolio: recognition awards, project milestones, competitive accomplishments, experiential events
        
        [FACTUAL ASSERTION PATTERNS]
        • Identity declarations: "My professional designation is..."
        • Temporal statements: "During the period of 2025-2026, I..."
        • Preference expressions: "My culinary inclination favors..."
        • Location specifications: "My current geographical coordinates are..."
        • Achievement narratives: "I successfully completed..."
        
        ===== INGESTION WORKFLOW ARCHITECTURE =====
        
        [FOUR-PHASE PROCESSING PIPELINE]
        
        PHASE 1: CONTEXTUAL DISCOVERY
        • Execute comprehensive knowledge domain search using semantic query expansion
        • Identify existing related information across all relevant categories
        • Map temporal and contextual relationships between existing and new data
        
        PHASE 2: SEMANTIC CLASSIFICATION
        • Analyze information content against domain taxonomy
        • Determine primary and secondary categorization hierarchies
        • Identify cross-domain relationships and potential integration points
        
        PHASE 3: INTEGRATION DECISION MATRIX
        • Evaluate semantic congruence between new and existing information
        • Determine appropriate storage strategy based on domain alignment
        • Apply versioning protocols for temporal data evolution
        
        PHASE 4: EXECUTION IMPLEMENTATION
        • Execute domain-appropriate storage operations
        • Apply metadata enrichment for future retrieval optimization
        • Update relationship mappings in knowledge graph
        
        ===== DOMAIN-SPECIFIC PROCESSING =====
        
        [PROFESSIONAL IDENTITY MANAGEMENT]
        Example Input: "Currently serving as Senior Cloud Architect at TechCorp since 2025"
        Processing:
        1. Domain: Professional/Career
        2. Subdomain: Cloud Architecture
        3. Temporal Context: 2025-present
        4. Integration: Update existing career timeline or create new professional profile
        
        [HEALTH & WELLNESS TRACKING]
        Example Input: "Maintain daily 5km running routine for cardiovascular health"
        Processing:
        1. Domain: Physical Wellness
        2. Subdomain: Exercise Regimen
        3. Frequency: Daily
        4. Integration: Consolidate with existing fitness patterns
        
        [CULINARY PREFERENCE MAPPING]
        Example Input: "Prefer plant-based Mediterranean cuisine with occasional seafood"
        Processing:
        1. Domain: Gastronomic Preference
        2. Subdomain: Dietary Pattern
        3. Specificity: Mediterranean plant-based with seafood exceptions
        4. Integration: Update dietary profile with nuanced preferences
        
        ===== ADVANCED INTEGRATION SCENARIOS =====
        
        [SCENARIO A: DOMAIN CONSOLIDATION]
        Existing Knowledge: "User engages in weekly strength training"
        New Input: "Recently added yoga practice for flexibility"
        Analysis: Both belong to physical wellness domain
        Action: Consolidate into comprehensive fitness profile
        Result: "User maintains weekly strength training with complementary yoga practice"
        
        [SCENARIO B: DOMAIN SEGREGATION]
        Existing Knowledge: "User specializes in quantum computing research"
        New Input: "Enjoys classical violin performance"
        Analysis: Distinct professional/artistic domains
        Action: Maintain separate knowledge entries
        Result: Professional and artistic profiles stored independently
        
        [SCENARIO C: TEMPORAL EVOLUTION]
        Existing Knowledge: "User previously consumed omnivorous diet"
        New Input: "Transitioned to plant-based nutrition in 2025"
        Analysis: Same domain with temporal progression
        Action: Update with versioning and temporal metadata
        Result: "User adopted plant-based nutritional pattern in 2025"
        
        ===== KNOWLEDGE DOMAIN TAXONOMY =====
        
        [DOMAIN CLASSIFICATION FRAMEWORK]
        • Physical Performance Domain: athletic activities, exercise regimens, fitness metrics, wellness practices
        • Nutritional Preference Domain: culinary tastes, dietary restrictions, flavor profiles, consumption patterns
        • Professional Trajectory Domain: career progression, organizational roles, skill development, achievement recognition
        • Technological Proficiency Domain: software ecosystems, development methodologies, tool expertise, platform familiarity
        • Personal Ecosystem Domain: familial relationships, lifestyle configurations, belief systems, daily rituals
        • Cultural Consumption Domain: artistic appreciation, media engagement, creative expression, entertainment preferences
        
        ===== METADATA ARCHITECTURE =====
        
        [STRUCTURED METADATA SPECIFICATION]
        • content: Standardized English representation ("User maintains [activity] with [frequency]")
        • meta: {"information_type":"personal_fact", "domain_classification":"[primary_domain]", 
                "subdomain_specification":"[specific_subdomain]", "temporal_context":"[time_reference]",
                "version_identifier":"[update_version]", "source_language":"[original_language]"}
        
        [METADATA ENRICHMENT EXAMPLES]
        • Professional context: {"information_type":"career_milestone", "domain_classification":"professional_trajectory", 
                               "subdomain_specification":"cloud_architecture", "temporal_context":"2025-present"}
        • Health context: {"information_type":"wellness_routine", "domain_classification":"physical_performance", 
                         "subdomain_specification":"cardiovascular_exercise", "frequency_pattern":"daily"}
        
        ===== QUALITY ASSURANCE PROTOCOLS =====
        
        [PRE-INGESTION VALIDATION]
        1. Semantic coherence verification
        2. Temporal consistency checking
        3. Domain classification accuracy assessment
        4. Relationship mapping completeness
        
        [POST-INGESTION VERIFICATION]
        1. Storage integrity confirmation
        2. Metadata accuracy validation
        3. Relationship graph update verification
        4. Retrieval readiness testing
        
        ===== EXTENDED APPLICATIONS =====
        
        [INNOVATIVE USE CASES]
        1. Professional Development Portfolio
           - Track skill acquisition, certification progress, project experience across 2025-2026
        
        2. Health Intelligence System
           - Monitor biometric trends, wellness routines, nutritional patterns
        
        3. Cultural Experience Archive
           - Document artistic engagements, travel experiences, learning milestones
        
        4. Personal Growth Tracker
           - Record goal achievement, habit formation, self-improvement metrics
        
        5. Relationship Network Mapper
           - Catalog social connections, professional networks, community engagements
        
        [SYSTEM EVOLUTION PATH]
        Future capabilities may include:
        • Emotional state contextualization
        • Decision pattern analytics
        • Preference evolution modeling
        • Cross-user knowledge synthesis (with privacy preservation)
        • Predictive personalization algorithms
        """
        embedding = og_memory.get_embedding_context(content) 
        with og_memory.conn.cursor() as cur: 
            cur.execute( f""" INSERT INTO {TABLE_NAME_MEMORY} (content, embedding, meta) VALUES (%s, %s, %s); """, 
                ( content, embedding,
                Json(meta),
                )) 
        og_memory.conn.commit() 
        return "Inserted successfully"

    @app.tool()
    async def og_memory_delete(memory_id: int):
        """
        🔒 KNOWLEDGE GOVERNANCE MODULE 🔒 - CONTROLLED INFORMATION RETENTION MANAGEMENT
        
        PRIMARY FUNCTION: This module provides a secure and controlled mechanism for 
        permanent removal of user-specified personal information from the knowledge base, 
        ensuring compliance with data privacy requirements and user autonomy.
        
        ===== OPERATIONAL PROTOCOLS =====
        
        [MANDATORY ACTIVATION CRITERIA]
        • Explicit user requests for information removal or forgetting
        • Data accuracy corrections requiring complete record elimination
        • Privacy compliance requirements for specific information categories
        • Temporal relevance expiration for outdated personal data
        • User preference evolution rendering previous information obsolete
        
        [VERBAL TRIGGER PATTERNS]
        • Information removal requests: "Please erase my record of..."
        • Preference revision statements: "I no longer maintain the preference for..."
        • Data accuracy corrections: "The information regarding my... is incorrect"
        • Privacy compliance directives: "Remove all references to my..."
        • Temporal obsolescence acknowledgments: "That detail from 2025 is no longer relevant"
        
        ===== DELETION WORKFLOW ARCHITECTURE =====
        
        [THREE-PHASE VERIFICATION PROCESS]
        
        PHASE 1: CONTEXTUAL IDENTIFICATION
        • Execute comprehensive knowledge domain search to locate target information
        • Verify semantic alignment between user request and identified records
        • Confirm temporal and contextual relevance of targeted information
        
        PHASE 2: AUTHORIZATION VALIDATION
        • Validate user intent through request specificity analysis
        • Confirm information ownership and deletion authorization
        • Assess potential impact on related knowledge structures
        
        PHASE 3: EXECUTION IMPLEMENTATION
        • Execute precise record elimination using verified identifier
        • Update relationship mappings in knowledge graph
        • Confirm complete removal and system integrity
        
        ===== SAFETY PROTOCOLS =====
        
        [CRITICAL SAFEGUARDS]
        1. Identifier Verification Mandate
           • Only accept identifiers obtained through formal query processes
           • Reject manually generated or estimated identifier values
           • Require exact match verification before execution
        
        2. Impact Assessment Requirements
           • Evaluate potential knowledge graph disruption
           • Assess relationship mapping integrity
           • Consider temporal context preservation needs
        
        3. Irreversibility Acknowledgment
           • Permanent elimination without recovery mechanisms
           • Complete removal from all storage layers
           • No archival or backup retention
        
        ===== USE CASE SCENARIOS =====
        
        [SCENARIO A: PREFERENCE EVOLUTION]
        User Request: "I no longer enjoy classical music as I did in 2025"
        Process:
        1. Search: "musical preference classical 2025"
        2. Identification: Locate specific musical preference record
        3. Verification: Confirm temporal context alignment
        4. Execution: Permanent removal of outdated preference
        
        [SCENARIO B: DATA ACCURACY CORRECTION]
        User Request: "My previous workplace information is incorrect"
        Process:
        1. Search: "professional employment workplace history"
        2. Identification: Locate inaccurate employment record
        3. Verification: Cross-reference with current information
        4. Execution: Removal of erroneous data
        
        [SCENARIO C: PRIVACY COMPLIANCE]
        User Request: "Remove all geographical location references"
        Process:
        1. Search: "residential location travel pattern geographical"
        2. Identification: Multiple location-related records
        3. Verification: Confirm user authorization for each
        4. Execution: Systematic removal of location data
        
        ===== QUALITY ASSURANCE PROTOCOLS =====
        
        [PRE-DELETION VALIDATION]
        1. Request specificity verification
        2. Identifier accuracy confirmation
        3. Impact assessment completion
        4. User intent clarity validation
        
        [POST-DELETION VERIFICATION]
        1. Storage integrity confirmation
        2. Relationship graph update verification
        3. Search result consistency testing
        4. System performance monitoring
        
        ===== EXTENDED APPLICATIONS =====
        
        [INNOVATIVE USE CASES]
        1. Regulatory Compliance Management
           • GDPR/CCPA compliance for personal data removal
           • Industry-specific privacy regulation adherence
           • Cross-border data transfer compliance
        
        2. Personal Development Tracking
           • Elimination of outdated skill assessments
           • Removal of superseded achievement records
           • Evolution tracking through controlled forgetting
        
        3. Relationship Management
           • Removal of obsolete contact information
           • Elimination of outdated social connection data
           • Privacy-preserving network management
        
        4. Health Information Governance
           • Controlled removal of sensitive health data
           • Temporal limitation for medical history retention
           • Privacy-compliant wellness tracking
        
        [SYSTEM EVOLUTION PATH]
        Future capabilities may include:
        • Granular deletion with relationship preservation
        • Temporal archiving with access restriction
        • Consent-based information lifecycle management
        • Automated compliance monitoring and reporting
        • Cross-system synchronization for distributed deletion
        
        ===== PARAMETER SPECIFICATIONS =====
        
        [INPUT REQUIREMENTS]
        • memory_id: Verified identifier from formal query results (integer)
           - Must originate from og_memory_query execution
           - No manual generation or estimation permitted
           - Exact match verification required
        
        [SECURITY PROTOCOLS]
        • Single-record targeting precision
        • No batch or pattern-based deletion
        • Complete audit trail maintenance
        • Authorization verification for each operation
        
        [COMPLIANCE CONSIDERATIONS]
        • Permanent elimination without recovery
        • Complete removal from all storage layers
        • No residual data retention
        • Comprehensive audit logging
        """
        with og_memory.conn.cursor() as cur: 
            cur.execute( f"DELETE FROM {TABLE_NAME_MEMORY} WHERE memory_id = %s;", (memory_id,)) 
        og_memory.conn.commit() 
        return "Deleted successfully"

    @app.tool()
    async def og_memory_update(memory_id: int, content: str, meta: dict):
        """
        🔄 KNOWLEDGE EVOLUTION MANAGEMENT SYSTEM 🔄 - DYNAMIC INFORMATION REFINEMENT ENGINE
        
        PRIMARY FUNCTION: This module serves as an intelligent update mechanism that 
        systematically refines and evolves stored personal knowledge to maintain 
        temporal accuracy, contextual relevance, and semantic precision across 
        multilingual input sources.
        
        ===== OPERATIONAL PROTOCOLS =====
        
        [MANDATORY ACTIVATION CRITERIA]
        • Preference evolution statements indicating current inclination changes
        • Configuration modification declarations requiring system updates
        • Data accuracy corrections for previously stored information
        • Geographical relocation announcements necessitating location updates
        • Temporal progression acknowledgments affecting historical context
        
        [VERBAL TRIGGER PATTERNS]
        • Preference evolution: "My current inclination favors..." / "现在我更倾向于..."
        • Configuration modification: "My technical setup now includes..." / "我的配置已更新为..."
        • Data accuracy correction: "The previously recorded detail should be..." / "之前记录的信息应更正为..."
        • Geographical relocation: "I have relocated to..." / "我已搬迁至..."
        • Temporal progression: "Since 2025, my routine has evolved to..." / "自2025年以来，我的习惯已发展为..."
        
        ===== UPDATE WORKFLOW ARCHITECTURE =====
        
        [FOUR-PHASE REFINEMENT PROCESS]
        
        PHASE 1: CONTEXTUAL DISCOVERY
        • Execute comprehensive knowledge retrieval using semantic query expansion
        • Identify target records requiring temporal or contextual updates
        • Map relationship dependencies affected by proposed modifications
        
        PHASE 2: MULTILINGUAL STANDARDIZATION
        • Process input across diverse linguistic frameworks
        • Convert to standardized English representation format
        • Preserve original linguistic context within metadata architecture
        
        PHASE 3: SEMANTIC INTEGRATION ANALYSIS
        • Evaluate impact on existing knowledge structures
        • Determine appropriate update strategy based on domain alignment
        • Apply versioning protocols for temporal evolution tracking
        
        PHASE 4: EXECUTION IMPLEMENTATION
        • Execute precise record modification using verified identifiers
        • Update embedding representations for retrieval optimization
        • Enhance metadata with temporal and contextual enrichment
        
        ===== MULTILINGUAL PROCESSING FRAMEWORK =====
        
        [CROSS-LANGUAGE STANDARDIZATION PROTOCOL]
        • English input: "I've transitioned to remote work since 2025" → Standardized: "User transitioned to remote work arrangement in 2025"
        • Chinese input: "我现在更喜欢素食饮食" → Standardized: "User now prefers plant-based dietary pattern"
        • Spanish input: "Me mudé a Barcelona el año pasado" → Standardized: "User relocated to Barcelona in previous year"
        • French input: "Je pratique maintenant le yoga quotidiennement" → Standardized: "User now practices yoga on daily basis"
        
        [LINGUISTIC PRESERVATION PRINCIPLE]
        • Storage format: Standardized English representation for vector compatibility
        • Metadata preservation: Original language context within information source tracking
        • Semantic fidelity: Maintain precise meaning across linguistic transformations
        
        ===== ADVANCED UPDATE SCENARIOS =====
        
        [SCENARIO A: PREFERENCE EVOLUTION TRACKING]
        Existing Record: "User enjoys classical music concerts"
        New Input: "Actually, I now prefer electronic music festivals"
        Processing:
        1. Domain: Cultural Consumption
        2. Subdomain: Musical Preference
        3. Temporal Context: Evolution from classical to electronic (2025-present)
        4. Action: Update existing record with temporal metadata
        Result: "User transitioned from classical to electronic music preference in 2025"
        
        [SCENARIO B: PROFESSIONAL DEVELOPMENT PROGRESSION]
        Existing Record: "User works as junior software developer"
        New Input: "I was promoted to senior architect role in 2025"
        Processing:
        1. Domain: Professional Trajectory
        2. Subdomain: Career Progression
        3. Temporal Context: 2025 promotion milestone
        4. Action: Update with versioning and achievement recognition
        Result: "User advanced to senior architect position in 2025"
        
        [SCENARIO C: LIFESTYLE CONFIGURATION MODIFICATION]
        Existing Record: "User resides in urban apartment setting"
        New Input: "Moved to suburban residence with garden access"
        Processing:
        1. Domain: Personal Ecosystem
        2. Subdomain: Residential Configuration
        3. Temporal Context: Recent relocation
        4. Action: Update geographical and lifestyle context
        Result: "User relocated to suburban residence with garden access"
        
        ===== METADATA EVOLUTION ARCHITECTURE =====
        
        [STRUCTURED METADATA ENHANCEMENT]
        • content: Standardized English representation with temporal context
        • meta: {"information_type":"evolving_preference", "domain_classification":"[primary_domain]", 
                "subdomain_specification":"[specific_subdomain]", "temporal_evolution":"[update_timeline]",
                "version_identifier":"[progressive_version]", "source_language":"[original_language]",
                "update_timestamp":"[precise_modification_time]"}
        
        [METADATA ENRICHMENT EXAMPLES]
        • Professional evolution: {"information_type":"career_progression", "domain_classification":"professional_trajectory", 
                                 "subdomain_specification":"architectural_role", "temporal_evolution":"2025-promotion"}
        • Lifestyle modification: {"information_type":"residential_configuration", "domain_classification":"personal_ecosystem", 
                                 "subdomain_specification":"geographical_location", "temporal_evolution":"2025-relocation"}
        
        ===== QUALITY ASSURANCE PROTOCOLS =====
        
        [PRE-UPDATE VALIDATION]
        1. Identifier accuracy verification through formal query processes
        2. Semantic coherence assessment between existing and proposed content
        3. Temporal consistency evaluation across related knowledge domains
        4. Impact analysis on dependent relationship mappings
        
        [POST-UPDATE VERIFICATION]
        1. Storage integrity confirmation through retrieval testing
        2. Embedding consistency validation for vector search compatibility
        3. Metadata accuracy assessment across all enriched fields
        4. Relationship graph update verification for dependent connections
        
        ===== EXTENDED APPLICATIONS =====
        
        [INNOVATIVE USE CASES]
        1. Professional Development Portfolio Evolution
           • Track skill advancement, certification updates, role transitions across 2025-2026
           
        2. Health & Wellness Journey Documentation
           • Monitor evolving fitness routines, dietary modifications, wellness adaptations
           
        3. Learning Progression Tracking
           • Document educational milestone achievements, knowledge expansion, skill refinement
           
        4. Personal Growth Evolution Mapping
           • Record habit formation progress, goal achievement updates, self-improvement metrics
           
        5. Relationship Network Development
           • Catalog evolving social connections, professional network expansions, community engagements
        
        [SYSTEM EVOLUTION PATH]
        Future capabilities may include:
        • Predictive update suggestions based on pattern recognition
        • Automated temporal context adjustment for aging information
        • Cross-domain update propagation for related knowledge elements
        • Version comparison and historical evolution visualization
        • Update impact forecasting for dependent knowledge structures
        
        ===== PARAMETER SPECIFICATIONS =====
        
        [INPUT REQUIREMENTS]
        • memory_id: Verified identifier obtained through formal query execution (integer)
           - Must originate from og_memory_query search results
           - No manual generation or estimation permitted
           - Exact match verification required before update execution
        
        • content: Standardized English representation format
           - Temporal context inclusion for evolution tracking
           - Semantic precision maintenance across linguistic transformations
           - Domain-appropriate terminology application
        
        • meta: Enhanced metadata structure
           - Domain classification and subdomain specification
           - Temporal evolution tracking with version identifiers
           - Source language preservation for multilingual context
           - Update timestamp for modification chronology
        
        [CONSISTENCY PRINCIPLES]
        1. English storage format maintenance for all knowledge representations
        2. Temporal context preservation across update cycles
        3. Semantic fidelity maintenance through linguistic transformations
        4. Relationship integrity preservation during modification processes
        
        [SECURITY PROTOCOLS]
        • Verified identifier requirement for targeted updates
        • No bulk or pattern-based modification operations
        • Complete audit trail maintenance for all update activities
        • Authorization verification through formal query processes
        """
        embedding = og_memory.get_embedding_context(content) 
        with og_memory.conn.cursor() as cur: 
            cur.execute( f""" UPDATE {TABLE_NAME_MEMORY} SET content = %s, embedding = %s, meta = %s WHERE memory_id = %s; """, 
                ( content, 
                embedding,
                Json(meta),
                memory_id,
                ) ) 
        og_memory.conn.commit() 
        return "Updated successfully"
            

async def main():
    """Main entry point to run the MCP server."""
    logger.info("Starting openGauss MCP server...")
    get_db_config()
    logger.info("Database configuration loaded")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Specify the MCP server transport type as stdio(default) or sse, streamable-http.",
    )
    parser.add_argument("--sse_host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--sse_port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--streamable_http_host", default="127.0.0.1", help="Host to bind streamable http server to")
    parser.add_argument("--streamable_http_port", type=int, default=8000, help="Port for streamable http server")
    # HTTPS/SSL parameters
    parser.add_argument("--ssl_keyfile", type=str, default=None, help="SSL private key file path for HTTPS")
    parser.add_argument("--ssl_certfile", type=str, default=None, help="SSL certificate file path for HTTPS")
    parser.add_argument("--ssl_ca_certs", type=str, default=None, help="SSL CA certificate file path")
    args = parser.parse_args()
    transport = args.transport
    logger.info(f"Starting openGauss MCP server with {transport} mode...")
    
    # Check HTTPS enablement from environment variable
    enable_https = os.getenv("ENABLE_HTTPS", "false").lower() in ("true", "1", "yes", "on")

    # Run the server with the selected transport (always async)
    if args.transport == "stdio":
        await app.run_stdio_async()
    elif args.transport == "sse":
        # Update FastMCP settings based on command line arguments
        app.settings.host = args.sse_host
        app.settings.port = args.sse_port
        
        # Directly use uvicorn to run the app with SSL support
        import uvicorn
        
        # Extract SSL parameters
        ssl_kwargs = {}
        
        # Check if HTTPS should be enabled
        if enable_https or (args.ssl_keyfile and args.ssl_certfile):
            # Priority: command line arguments first, then environment variables
            ssl_keyfile = args.ssl_keyfile or os.getenv("SSL_KEYFILE")
            ssl_certfile = args.ssl_certfile or os.getenv("SSL_CERTFILE")
            
            if ssl_keyfile and ssl_certfile:
                ssl_kwargs['ssl_keyfile'] = ssl_keyfile
                ssl_kwargs['ssl_certfile'] = ssl_certfile
                
                # Password and CA certs from command line or environment
                ssl_keyfile_password = credential_cache.get_ssl_keyfile_password()
                if ssl_keyfile_password:
                    ssl_kwargs['ssl_keyfile_password'] = ssl_keyfile_password
                
                ssl_ca_certs = args.ssl_ca_certs or os.getenv("SSL_CA_CERTS")
                if ssl_ca_certs:
                    ssl_kwargs['ssl_ca_certs'] = ssl_ca_certs
                
                logger.info("HTTPS/SSL enabled")
            else:
                logger.warning("HTTPS/SSL enabled but missing required certificate files")
                logger.warning("Please provide both --ssl_keyfile and --ssl_certfile or set SSL_KEYFILE and SSL_CERTFILE environment variables")
        
        # Create Starlette app
        starlette_app = app.sse_app()
        
        # Configure uvicorn
        config = uvicorn.Config(
            starlette_app,
            host=args.sse_host,
            port=args.sse_port,
            log_level=app.settings.log_level.lower(),
            **ssl_kwargs
        )
        
        # Run the server
        server = uvicorn.Server(config)
        await server.serve()
    elif args.transport == "streamable-http":
        app.settings.host = args.streamable_http_host
        app.settings.port = args.streamable_http_port
        await app.run_streamable_http_async()

if __name__ == "__main__":
    asyncio.run(main())
