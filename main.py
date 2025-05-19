from mcp.server.fastmcp import FastMCP, Context
import json
import os
import sys
from typing import Dict, List, AsyncIterator, Optional
import time
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

from db_context import DatabaseContext
from db_context.schema.formatter import format_select_results

# Load environment variables from .env file
load_dotenv()

ORACLE_CONNECTION_STRING = os.getenv('ORACLE_CONNECTION_STRING')
TARGET_SCHEMA = os.getenv('TARGET_SCHEMA')  # Optional schema override
CACHE_DIR = os.getenv('CACHE_DIR', '.cache')
USE_THICK_MODE = os.getenv('THICK_MODE', '').lower() in ('true', '1', 'yes')  # Convert string to boolean
ORACLE_CLIENT_LIB_DIR = os.getenv('ORACLE_CLIENT_LIB_DIR', None)

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[DatabaseContext]:
    """Manage application lifecycle and ensure DatabaseContext is properly initialized"""
    print("App Lifespan initialising", file=sys.stderr)
    connection_string = ORACLE_CONNECTION_STRING
    if not connection_string:
        raise ValueError("ORACLE_CONNECTION_STRING environment variable is required. Set it in .env file or environment.")
    
    cache_dir = Path(CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    db_context = DatabaseContext(
        connection_string=connection_string,
        cache_path=cache_dir / 'schema_cache.json',
        target_schema=TARGET_SCHEMA,
        use_thick_mode=USE_THICK_MODE,  # Pass the thick mode setting
        lib_dir=ORACLE_CLIENT_LIB_DIR
    )
    
    try:
        # Initialize cache on startup
        print("Initialising database cache...", file=sys.stderr)
        await db_context.initialize()
        print("Cache ready!", file=sys.stderr)
        yield db_context
    finally:
        # Ensure proper cleanup of database resources
        print("Closing database connections...", file=sys.stderr)
        await db_context.close()
        print("Database connections closed", file=sys.stderr)

# Initialize FastMCP server
mcp = FastMCP("oracle", lifespan=app_lifespan)
print("FastMCP server initialized", file=sys.stderr)

@mcp.tool()
async def get_table_schema(table_name: str, ctx: Context) -> str:
    """
    Get the schema information for a specific table including columns, data types, nullability, and relationships.
    Use this when you need to understand the structure of a particular table to write queries against it or to analyze data models.
    This tool is particularly useful before writing complex SQL queries, designing new tables, or establishing relationships between existing tables.
    The table name parameter is case-insensitive, so 'CUSTOMERS', 'customers', and 'Customers' will all retrieve the same table.
    
    Args:
        table_name: The name of the table to get schema information for (case-insensitive). Must be an exact table name,
                   as this tool does not support partial matches or wildcards. For pattern matching, use search_tables_schema instead.
    
    Returns:
        A formatted string containing the table's schema information including columns (with data types and nullability)
        and relationships to other tables. Returns an error message if the table is not found in the database schema.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    table_info = await db_context.get_schema_info(table_name)
    
    if not table_info:
        return f"Table '{table_name}' not found in the schema."
    
    # Delegate formatting to the TableInfo model
    return table_info.format_schema()

@mcp.tool()
async def rebuild_schema_cache(ctx: Context) -> str:
    """
    Force a complete rebuild of the database schema cache. This operation is computationally expensive and time-consuming
    as it queries the database for metadata on all tables, columns, relationships, and constraints.
    Use this tool only when absolutely necessary, such as when database objects have been added, modified, or removed
    since the application started, or when you suspect the cache may be out of sync with the actual database schema.
    
    This operation can take several minutes for large databases with hundreds of tables and may impact
    performance of other operations while running. The schema cache is automatically built at startup, so
    this should only be used when explicitly needed during a session.
    
    Returns:
        A message indicating the result of the rebuild operation, including the number of tables indexed
        or an error message if the rebuild failed
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    try:
        await db_context.rebuild_cache()
        cache_size = len(db_context.schema_manager.cache.all_table_names) if db_context.schema_manager.cache else 0
        return f"Schema cache rebuilt successfully. Indexed {cache_size} tables."
    except Exception as e:
        return f"Failed to rebuild schema cache: {str(e)}"

@mcp.tool()
async def get_tables_schema(table_names: List[str], ctx: Context) -> str:
    """
    Get the schema information for multiple tables at once in a single database query.
    This tool is significantly more efficient than calling get_table_schema multiple times as it
    reduces network round-trips and database load. Use this tool whenever you need information
    about two or more tables, especially when analyzing relationships across tables or designing queries
    that join multiple tables.
    
    There is no hard limit on how many tables can be requested, but requesting too many large tables
    at once may cause performance issues. If a requested table doesn't exist, an error message for that
    specific table will be included in the results while still returning information for valid tables.
    
    Args:
        table_names: A list of table names to get schema information for (case-insensitive). Each name
                    must be exact, as this tool does not support partial matches or wildcards.
    
    Returns:
        A formatted string containing the schema information for all requested tables, including
        columns (with data types and nullability) and relationships for each table. Tables are
        grouped and clearly separated in the output.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    results = []
    
    for table_name in table_names:
        table_info = await db_context.get_schema_info(table_name)
        if not table_info:
            results.append(f"\nTable '{table_name}' not found in the schema.")
            continue
        
        # Delegate formatting to the TableInfo model
        results.append(table_info.format_schema())
    
    return "\n".join(results)

@mcp.tool()
async def search_tables_schema(search_term: str, ctx: Context) -> str:
    """
    Search for tables with names similar to the provided search terms and return their schema information.
    Multiple terms can be provided separated by commas or whitespace to find tables matching any of the terms.
    Use this tool when you aren't sure of the exact table name but know part of it, or when exploring tables 
    related to a specific domain or function like 'customer', 'order', or 'inventory'.
    
    The search is case-insensitive and matches substrings anywhere in the table name. For example, searching
    for 'cust' will match 'CUSTOMERS', 'customer_data', and 'historical_customer_orders'. Warning: results are limited
    to 20 tables total across all search terms to prevent overwhelming responses for generic terms. This means that if 
    too many tables are matched, only the first 20 will be returned, which may lead to missing very relevant tables. So, 
    if you encounter this, try to be more specific with your search terms and consider there may be more relevant tables.
    
    Args:
        search_term: One or more strings to search for in table names (case-insensitive), separated by commas or spaces.
                     Each term is treated as a separate search, with results combined (logical OR).
    
    Returns:
        A formatted string containing the schema information for all matching tables (up to 20 tables total),
        including column definitions and relationships for each table. If no matches are found, returns an
        error message listing which terms were searched.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    # Split search term by commas and whitespace and remove empty strings
    search_terms = [term.strip() for term in search_term.replace(',', ' ').split()]
    search_terms = [term for term in search_terms if term]
    
    if not search_terms:
        return "No valid search terms provided"
    
    # Track all matching tables without duplicates
    matching_tables = set()
    
    # Search for each term
    for term in search_terms:
        tables = await db_context.search_tables(term, limit=20)
        matching_tables.update(tables)
    
    # Convert back to list and limit to 20 results
    matching_tables = list(matching_tables)
    total_matches = len(matching_tables)
    limited_tables = matching_tables[:20]
    
    if not matching_tables:
        return f"No tables found matching any of these terms: {', '.join(search_terms)}"
    
    if total_matches > 20:
        results = [f"Found {total_matches} tables matching terms ({', '.join(search_terms)}). Returning the first 20 for performance reasons:"]
    else:
        results = [f"Found {total_matches} tables matching terms ({', '.join(search_terms)}):"]
    
    matching_tables = limited_tables
    
    # Now load the schema for each matching table
    for table_name in matching_tables:
        table_info = await db_context.get_schema_info(table_name)
        if not table_info:
            continue
        
        # Delegate formatting to the TableInfo model
        results.append(table_info.format_schema())
    
    return "\n".join(results)

@mcp.tool()
async def get_database_vendor_info(ctx: Context) -> str:
    """
    Returns the database vendor type and version by querying the connected Oracle database.
    This information is critical for writing database-specific SQL features and syntax that may vary between vendors 
    and versions (Oracle, MySQL, PostgreSQL, etc.) or even between different versions of the same database.
    Use this tool to determine which SQL dialect features are available and to ensure compatibility when 
    writing complex queries, stored procedures, or leveraging vendor-specific functionality.
    
    The tool attempts to return comprehensive information including the database vendor name, version number,
    current schema context, and additional version-specific details when available. This can help diagnose 
    connection issues or verify you're connected to the expected database environment.
    
    Returns:
        A formatted string containing the database vendor type, version information, current schema,
        and any additional version-specific details available from the database. Returns an error
        message if the database could not be queried successfully.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        db_info = await db_context.get_database_info()
        
        if not db_info:
            return "Could not retrieve database vendor information."
        
        result = [f"Database Vendor: {db_info.get('vendor', 'Unknown')}"]
        result.append(f"Version: {db_info.get('version', 'Unknown')}")
        if "schema" in db_info:
            result.append(f"Schema: {db_info['schema']}")
        
        if "additional_info" in db_info and db_info["additional_info"]:
            result.append("\nAdditional Version Information:")
            for info in db_info["additional_info"]:
                result.append(f"- {info}")
                
        if "error" in db_info:
            result.append(f"\nError: {db_info['error']}")
            
        return "\n".join(result)
    except Exception as e:
        return f"Error retrieving database vendor information: {str(e)}"

@mcp.tool()
async def search_columns(search_term: str, ctx: Context) -> str:
    """
    Search for tables containing columns that match the provided search term in their name.
    This tool is extremely useful when you know what data you need (like 'customer_id' or 'order_date') 
    but aren't sure which tables contain this information. Essential for exploring large databases and
    understanding data relationships without having to examine each table individually.
    
    The search is case-insensitive and matches substrings anywhere in the column name. Results are limited
    to 50 column matches across all tables to prevent overwhelming responses. For each matching column, the
    tool returns the table name, column name, data type, and nullability status, helping you identify
    the right tables to query for specific data.
    
    Args:
        search_term: A string to search for in column names (case-insensitive). For example, 'address',
                    'date', 'amount', etc. Does not support wildcards or regex patterns.
    
    Returns:
        A formatted string listing tables and their matching columns (up to 50 results) with data types
        and nullability information. Returns an error message if no matches are found or an error occurs.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        matching_columns = await db_context.search_columns(search_term, limit=50)
        
        if not matching_columns:
            return f"No columns found matching '{search_term}'"
        
        results = [f"Found columns matching '{search_term}' in {len(matching_columns)} tables:"]
        
        for table_name, columns in matching_columns.items():
            results.append(f"\nTable: {table_name}")
            results.append("Matching columns:")
            for col in columns:
                nullable = "NULL" if col["nullable"] else "NOT NULL"
                results.append(f"  - {col['name']}: {col['type']} {nullable}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error searching columns: {str(e)}"

@mcp.tool()
async def get_pl_sql_objects(object_type: str, name_pattern: Optional[str], ctx: Context) -> str:
    """
    Get information about PL/SQL objects (procedures, functions, packages, triggers, etc) in the database.
    Use this tool to discover existing database code objects for analysis, debugging, or understanding how
    the database implements business logic. This is particularly useful when working with an unfamiliar database
    or when trying to locate specific stored procedures or functions that need modification.
    
    The tool supports multiple object types including PROCEDURE, FUNCTION, PACKAGE, TRIGGER, TYPE, VIEW, 
    SEQUENCE, and others. Results include object names, status (valid/invalid), owner information, and 
    creation/modification dates when available. Results may be limited to prevent overwhelming responses for 
    generic patterns.
    
    Args:
        object_type: Type of object to search for (PROCEDURE, FUNCTION, PACKAGE, TRIGGER, TYPE, etc.)
                    Must be a valid database object type. The value is automatically converted to uppercase.
        name_pattern: Pattern to filter object names (case-insensitive, supports % wildcards).
                     e.g., "CUSTOMER%" will find all objects starting with "CUSTOMER", "%ORDER%" will find 
                     objects containing "ORDER". If null or empty, all objects of the specified type are returned.
    
    Returns:
        A formatted string containing information about the matching PL/SQL objects, including their
        names, owners, status, and timestamps. Returns an error message if no matching objects are found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        objects = await db_context.get_pl_sql_objects(object_type.upper(), name_pattern)
        
        if not objects:
            pattern_msg = f" matching '{name_pattern}'" if name_pattern else ""
            return f"No {object_type.upper()} objects found{pattern_msg}"
        
        results = [f"Found {len(objects)} {object_type.upper()} objects:"]
        
        for obj in objects:
            results.append(f"\n{obj['type']}: {obj['name']}")
            if 'owner' in obj:
                results.append(f"Owner: {obj['owner']}")
            if 'status' in obj:
                results.append(f"Status: {obj['status']}")
            if 'created' in obj:
                results.append(f"Created: {obj['created']}")
            if 'last_modified' in obj:
                results.append(f"Last Modified: {obj['last_modified']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving PL/SQL objects: {str(e)}"

@mcp.tool()
async def get_object_source(object_type: str, object_name: str, ctx: Context) -> str:
    """
    Get the source code for a PL/SQL object (procedure, function, package, trigger, etc.).
    Essential for debugging, understanding, or optimizing existing database code. Use this tool
    when you need to analyze how a database object is implemented, understand its business logic,
    or prepare to modify an existing database procedure or function.
    
    The tool retrieves the complete source code with all comments and formatting preserved. For packages,
    both the specification (header) and body are returned when available. Note that the user must have
    appropriate database permissions to view the source code of objects, particularly those owned by 
    different schemas.
    
    Args:
        object_type: Type of object (PROCEDURE, FUNCTION, PACKAGE, TRIGGER, etc.) to retrieve. 
                    Value is automatically converted to uppercase.
        object_name: Name of the object to retrieve source for. Value is automatically converted to uppercase.
                    Must be an exact object name (no wildcards or partial matching).
    
    Returns:
        A string containing the complete source code of the requested object with original formatting
        preserved. Returns an error message if the object does not exist, the user lacks permissions
        to view it, or an error occurs during retrieval.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        source = await db_context.get_object_source(object_type.upper(), object_name.upper())
        
        if not source:
            return f"No source found for {object_type} {object_name}"
        
        return f"Source for {object_type} {object_name}:\n\n{source}"
    except Exception as e:
        return f"Error retrieving object source: {str(e)}"

@mcp.tool()
async def get_table_constraints(table_name: str, ctx: Context) -> str:
    """
    Get constraints (primary keys, foreign keys, unique constraints, check constraints) for a table.
    Use this to understand the data integrity rules, relationships, and business rules encoded in the database.
    Critical for writing valid INSERT/UPDATE statements and understanding join conditions. Different constraint
    types serve different purposes: primary keys uniquely identify rows, foreign keys establish relationships
    between tables, unique constraints ensure distinct values, and check constraints enforce business rules.
    
    This tool returns all constraints defined on the table including their names, types, and affected columns.
    For foreign keys, it also shows which table and column(s) they reference, essential for understanding
    the database's relational structure. For check constraints, the actual validation condition is included.
    
    Args:
        table_name: The name of the table to get constraints for (case-insensitive). Must be an exact table name.
    
    Returns:
        A formatted string containing the table's constraints with detailed information including constraint
        names, types, columns, and referenced objects. Returns an error message if the table has no constraints
        or if an error occurs during retrieval.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        constraints = await db_context.get_table_constraints(table_name)
        
        if not constraints:
            return f"No constraints found for table '{table_name}'"
        
        results = [f"Constraints for table '{table_name}':"]
        
        for constraint in constraints:
            constraint_type = constraint.get('type', 'UNKNOWN')
            name = constraint.get('name', 'UNNAMED')
            
            results.append(f"\n{constraint_type} Constraint: {name}")
            
            if 'columns' in constraint:
                results.append(f"Columns: {', '.join(constraint['columns'])}")
                
            if constraint_type == 'FOREIGN KEY' and 'references' in constraint:
                ref = constraint['references']
                results.append(f"References: {ref['table']}({', '.join(ref['columns'])})")
                
            if 'condition' in constraint:
                results.append(f"Condition: {constraint['condition']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving constraints: {str(e)}"

@mcp.tool()
async def get_table_indexes(table_name: str, ctx: Context) -> str:
    """
    Get indexes defined on a table to understand and optimize query performance. 
    Essential for query optimization and understanding performance characteristics of the table.
    Use this information when diagnosing slow queries, optimizing SELECT statements, or deciding 
    whether to create new indexes for performance improvements.
    
    The tool returns all indexes on the specified table, including their names, column lists, uniqueness flag,
    tablespace information, and status. Understanding indexes is critical for performance tuning as they 
    significantly affect how quickly data can be retrieved, especially for large tables. Regular indexes
    speed up searches, while unique indexes also enforce data uniqueness constraints.
    
    Args:
        table_name: The name of the table to get indexes for (case-insensitive). Must be an exact table name.
    
    Returns:
        A formatted string containing the table's indexes including column information, uniqueness flags,
        tablespace information, and status. Returns an error message if the table has no indexes or if
        an error occurs during retrieval.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        indexes = await db_context.get_table_indexes(table_name)
        
        if not indexes:
            return f"No indexes found for table '{table_name}'"
        
        results = [f"Indexes for table '{table_name}':"]
        
        for idx in indexes:
            idx_type = "UNIQUE " if idx.get('unique', False) else ""
            results.append(f"\n{idx_type}Index: {idx['name']}")
            results.append(f"Columns: {', '.join(idx['columns'])}")
            
            if 'tablespace' in idx:
                results.append(f"Tablespace: {idx['tablespace']}")
                
            if 'status' in idx:
                results.append(f"Status: {idx['status']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving indexes: {str(e)}"

@mcp.tool()
async def get_dependent_objects(object_name: str, ctx: Context) -> str:
    """
    Get objects that depend on the specified object (find usage references) in the database.
    This tool is crucial for impact analysis before modifying or dropping database objects,
    as it shows all other objects that will be affected by changes. Use this when planning database
    refactoring, identifying critical objects, or investigating complex dependencies.
    
    Dependencies include objects like views that reference a table, procedures that call other procedures,
    triggers that reference tables or columns, and any other database object that relies on the specified object.
    Understanding these dependencies helps prevent breaking changes and cascading failures in database applications.
    
    Args:
        object_name: Name of the object to find dependencies for (case-insensitive). The value is automatically
                    converted to uppercase. Must be an exact object name with no wildcards.
    
    Returns:
        A formatted string containing objects that depend on the specified object, including their types,
        names, and owner information when available. Returns an error message if no dependent objects
        are found or if an error occurs during retrieval.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        dependencies = await db_context.get_dependent_objects(object_name.upper())
        
        if not dependencies:
            return f"No objects found that depend on '{object_name}'"
        
        results = [f"Objects that depend on '{object_name}':"]
        
        for dep in dependencies:
            results.append(f"\n{dep['type']}: {dep['name']}")
            if 'owner' in dep:
                results.append(f"Owner: {dep['owner']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving dependencies: {str(e)}"

@mcp.tool()
async def get_user_defined_types(type_pattern: Optional[str], ctx: Context) -> str:
    """
    Get information about user-defined types in the database schema such as object types, nested tables,
    VARRAYs, and custom type definitions. Use this tool when working with complex data structures, stored
    procedures that use custom types, or when trying to understand the domain model implemented in the database.
    
    User-defined types are crucial for advanced database applications as they allow for complex data 
    modeling beyond simple scalar types. This tool shows the structure of these types including their
    attributes and type categories, helping you understand how to work with them in SQL queries or
    application code. The search is case-insensitive and supports wildcard patterns.
    
    Args:
        type_pattern: Pattern to filter type names (case-insensitive, supports % wildcards). For example,
                     "CUSTOMER%" will find types like CUSTOMER_TYPE, CUSTOMER_ADDRESS_TYPE, etc.
                     If null or empty, all user-defined types will be returned (may be a large list).
    
    Returns:
        A formatted string containing information about user-defined types, including name, type category,
        owner, and attributes when available. Returns an error message if no types are found matching
        the pattern or if an error occurs during retrieval.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        types = await db_context.get_user_defined_types(type_pattern)
        
        if not types:
            pattern_msg = f" matching '{type_pattern}'" if type_pattern else ""
            return f"No user-defined types found{pattern_msg}"
        
        results = [f"User-defined types:"]
        
        for typ in types:
            results.append(f"\nType: {typ['name']}")
            results.append(f"Type category: {typ['type_category']}")
            if 'owner' in typ:
                results.append(f"Owner: {typ['owner']}")
            if 'attributes' in typ and typ['attributes']:
                results.append("Attributes:")
                for attr in typ['attributes']:
                    results.append(f"  - {attr['name']}: {attr['type']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving user-defined types: {str(e)}"

@mcp.tool()
async def get_related_tables(table_name: str, ctx: Context) -> str:
    """
    Get all tables that are related to the specified table through foreign keys.
    This tool is critical for understanding the database schema relationships and building proper JOINs.
    Shows both tables referenced by this table (outgoing foreign keys) and tables that reference this table 
    (incoming foreign keys), providing a complete view of the table's place in the relational model.
    
    Understanding these relationships is essential for data navigation, ensuring referential integrity, and
    constructing efficient queries. Outgoing relationships show where this table depends on other tables,
    while incoming relationships show which tables depend on this one. This distinction is important when
    planning data modifications or understanding cascading effects of changes.
    
    Args:
        table_name: The name of the table to find relationships for (case-insensitive). 
                   Must be an exact table name with no wildcards.
    
    Returns:
        A formatted string showing all related tables in both directions (incoming and outgoing relationships),
        clearly distinguishing between tables referenced by this table and tables that reference this table.
        Returns an error message if no relationships exist or if an error occurs during retrieval.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        related = await db_context.get_related_tables(table_name)
        
        if not related['referenced_tables'] and not related['referencing_tables']:
            return f"No related tables found for '{table_name}'"
        
        results = [f"Tables related to '{table_name}':"]
        
        if related['referenced_tables']:
            results.append("\nTables referenced by this table (outgoing foreign keys):")
            for table in related['referenced_tables']:
                results.append(f"  - {table}")
        
        if related['referencing_tables']:
            results.append("\nTables that reference this table (incoming foreign keys):")
            for table in related['referencing_tables']:
                results.append(f"  - {table}")
        
        return "\n".join(results)
        
    except Exception as e:
        return f"Error getting related tables: {str(e)}"

@mcp.tool()
async def execute_select_query(query: str, ctx: Context) -> str:
    """
    Execute a SELECT SQL query and return the results as a formatted table. Only queries starting with SELECT are allowed.
    Args:
        query: The SQL SELECT query to execute. Must start with SELECT (case-insensitive).
    Returns:
        A formatted string table of the query results, or an error message if the query is invalid or fails.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    if not query.strip().lower().startswith("select"):
        return "Error: Only SELECT queries are allowed."
    try:
        rows = await db_context.execute_select_query(query)
        return format_select_results(rows)
    except Exception as e:
        return f"Error executing query: {str(e)}"

@mcp.tool()
async def execute_update_query(query: str, ctx: Context) -> str:
    """
    Execute an UPDATE SQL query and return the number of affected rows. Only queries starting with UPDATE are allowed.
    Args:
        query: The SQL UPDATE query to execute. Must start with UPDATE (case-insensitive).
    Returns:
        A string indicating the number of affected rows, or an error message if the query is invalid or fails.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    if not query.strip().lower().startswith("update"):
        return "Error: Only UPDATE queries are allowed."
    try:
        count = await db_context.execute_update_query(query)
        return f"Rows affected: {count}"
    except Exception as e:
        return f"Error executing update: {str(e)}"

@mcp.tool()
async def execute_delete_query(query: str, ctx: Context) -> str:
    """
    Execute a DELETE SQL query and return the number of affected rows. Only queries starting with DELETE are allowed.
    Args:
        query: The SQL DELETE query to execute. Must start with DELETE (case-insensitive).
    Returns:
        A string indicating the number of affected rows, or an error message if the query is invalid or fails.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    if not query.strip().lower().startswith("delete"):
        return "Error: Only DELETE queries are allowed."
    try:
        count = await db_context.execute_delete_query(query)
        return f"Rows affected: {count}"
    except Exception as e:
        return f"Error executing delete: {str(e)}"

if __name__ == "__main__":
    mcp.run()
