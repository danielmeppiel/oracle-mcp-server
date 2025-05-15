import sys
import oracledb
import time
import asyncio
from typing import Dict, List, Set, Optional, Any
from pathlib import Path
from .models import SchemaManager

class DatabaseConnector:
    def __init__(self, connection_string: str, target_schema: Optional[str] = None, use_thick_mode: bool = False, lib_dir: Optional[str] = None):
        self.connection_string = connection_string
        self.schema_manager: Optional[SchemaManager] = None  # Will be set by DatabaseContext
        self.target_schema: Optional[str] = target_schema
        self.thick_mode = use_thick_mode
        self._pool = None
        self._pool_lock = asyncio.Lock()
        
        if self.thick_mode:
            try:
                if lib_dir:
                    oracledb.init_oracle_client(lib_dir=lib_dir)
                else:
                    oracledb.init_oracle_client()
                print("Oracle Client initialized in thick mode", file=sys.stderr)
            except Exception as e:
                print(f"Warning: Could not initialize Oracle Client: {e}", file=sys.stderr)
                print("Falling back to thin mode", file=sys.stderr)
                self.thick_mode = False

    async def initialize_pool(self):
        """Initialize the connection pool"""
        async with self._pool_lock:
            if self._pool is None:
                try:
                    if self.thick_mode:
                        self._pool = oracledb.create_pool(
                            self.connection_string,
                            min=2,
                            max=10,
                            increment=1,
                            getmode=oracledb.POOL_GETMODE_WAIT
                        )
                    else:
                        self._pool = oracledb.create_pool_async(
                            self.connection_string,
                            min=2,
                            max=10,
                            increment=1,
                            getmode=oracledb.POOL_GETMODE_WAIT
                        )
                    print("Database connection pool initialized", file=sys.stderr)
                except Exception as e:
                    print(f"Error creating connection pool: {e}", file=sys.stderr)
                    raise

    async def get_connection(self):
        """Get a connection from the pool"""
        if self._pool is None:
            await self.initialize_pool()
            
        try:
            if self.thick_mode:
                return self._pool.acquire()
            else:
                return await self._pool.acquire()
        except Exception as e:
            print(f"Error acquiring connection from pool: {e}", file=sys.stderr)
            raise

    async def _close_connection(self, conn):
        """Return connection to the pool"""
        try:
            if self.thick_mode:
                self._pool.release(conn)
            else:
                await self._pool.release(conn)
        except Exception as e:
            print(f"Error releasing connection to pool: {e}", file=sys.stderr)

    async def close_pool(self):
        """Close the connection pool"""
        if self._pool:
            try:
                if self.thick_mode:
                    self._pool.close()
                else:
                    await self._pool.close()
                self._pool = None
                print("Connection pool closed", file=sys.stderr)
            except Exception as e:
                print(f"Error closing connection pool: {e}", file=sys.stderr)

    def set_schema_manager(self, schema_manager: SchemaManager) -> None:
        """Set the schema manager reference"""
        self.schema_manager = schema_manager

    async def _execute_cursor(self, cursor, sql: str, **params):
        """Helper method to execute cursor operations based on mode"""
        if self.thick_mode:
            cursor.execute(sql, **params)  # Synchronous execution
            return cursor.fetchall()
        else:
            await cursor.execute(sql, **params)  # Async execution
            return await cursor.fetchall()

    async def _execute_cursor_no_fetch(self, cursor, sql: str, **params):
        """Helper method for cursor operations that don't need fetching (e.g. DELETE, UPDATE)"""
        if self.thick_mode:
            cursor.execute(sql, **params)
        else:
            await cursor.execute(sql, **params)

    async def _commit(self, conn):
        """Commit the current transaction"""
        if self.thick_mode:
            conn.commit()
        else:         
            await conn.commit()


    async def _get_effective_schema(self, conn) -> str:
        """Get the effective schema to use (either target_schema or connection user)"""
        if self.target_schema:
            return self.target_schema.upper()
        return conn.username.upper()

    async def get_effective_schema(self) -> str:
        """Get the effective schema name (either target_schema or connection user)"""
        conn = await self.get_connection()
        try:
            return await self._get_effective_schema(conn)
        finally:
            await self._close_connection(conn)

    async def get_database_info(self) -> Dict[str, Any]:
        """Get information about the database vendor and version"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            # Query for database version information
            version_info = await self._execute_cursor(cursor, "SELECT * FROM v$version")
            
            # Extract vendor type and full version string
            vendor_info = {}
            
            if version_info:
                # First row typically contains the main Oracle version info
                full_version = version_info[0][0]
                vendor_info["vendor"] = "Oracle"
                vendor_info["version"] = full_version
                vendor_info["schema"] = await self._get_effective_schema(conn)
                
                # Additional version info rows
                additional_info = [row[0] for row in version_info[1:] if row[0]]
                if additional_info:
                    vendor_info["additional_info"] = additional_info
                    
            return vendor_info
        except oracledb.Error as e:
            print(f"Error getting database info: {str(e)}", file=sys.stderr)
            return {"vendor": "Oracle", "version": "Unknown", "error": str(e)}
        finally:
            await self._close_connection(conn)

    async def get_all_table_names(self) -> Set[str]:
        """Get a list of all table names in the database using optimized query"""
        conn = await self.get_connection()
        try:
            print("Getting list of all tables...", file=sys.stderr)
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            # Using RESULT_CACHE hint for frequently accessed data
            all_tables = await self._execute_cursor(
                cursor,
                """
                SELECT /*+ RESULT_CACHE */ table_name 
                FROM all_tables 
                WHERE owner = :owner
                ORDER BY table_name
                """,
                owner=schema
            )
            
            return {t[0] for t in all_tables}
        finally:
            await self._close_connection(conn)
    
    async def load_table_details(self, table_name: str) -> Optional[Dict[str, Any]]:
        """Load detailed schema information for a specific table with optimized queries"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            
            # Check if the table exists using result cache
            table_exists = await self._execute_cursor(
                cursor,
                """
                SELECT /*+ RESULT_CACHE */ COUNT(*) 
                FROM all_tables 
                WHERE owner = :owner AND table_name = :table_name
                """,
                owner=schema, 
                table_name=table_name.upper()
            )
            
            if table_exists[0][0] == 0:
                return None
                
            # Get column information using result cache and index hints
            columns = await self._execute_cursor(
                cursor,
                """
                SELECT /*+ RESULT_CACHE INDEX(atc) */ 
                    column_name, data_type, nullable
                FROM all_tab_columns atc
                WHERE owner = :owner AND table_name = :table_name
                ORDER BY column_id
                """,
                owner=schema, 
                table_name=table_name.upper()
            )
            
            column_info = []
            for column, data_type, nullable in columns:
                column_info.append({
                    "name": column,
                    "type": data_type,
                    "nullable": nullable == 'Y'
                })
            
            # Get relationship information using optimized join order and result cache
            relationships = await self._execute_cursor(
                cursor,
                """
                SELECT /*+ RESULT_CACHE */
                    'OUTGOING' AS relationship_direction,
                    acc.column_name AS source_column,
                    rcc.table_name AS referenced_table,
                    rcc.column_name AS referenced_column
                FROM all_constraints ac
                JOIN all_cons_columns acc ON acc.constraint_name = ac.constraint_name
                                        AND acc.owner = ac.owner
                JOIN all_cons_columns rcc ON rcc.constraint_name = ac.r_constraint_name
                                        AND rcc.owner = ac.r_owner
                WHERE ac.constraint_type = 'R'
                AND ac.owner = :owner
                AND ac.table_name = :table_name

                UNION ALL

                SELECT /*+ RESULT_CACHE */
                    'INCOMING' AS relationship_direction,
                    rcc.column_name AS source_column,
                    ac.table_name AS referenced_table,
                    acc.column_name AS referenced_column
                FROM all_constraints ac
                JOIN all_cons_columns acc ON acc.constraint_name = ac.constraint_name
                                        AND acc.owner = ac.owner
                JOIN all_cons_columns rcc ON rcc.constraint_name = ac.r_constraint_name
                                        AND rcc.owner = ac.r_owner
                WHERE ac.constraint_type = 'R'
                AND ac.r_owner = :owner
                AND ac.r_constraint_name IN (
                    SELECT constraint_name 
                    FROM all_constraints
                    WHERE owner = :owner
                    AND table_name = :table_name
                    AND constraint_type IN ('P', 'U')
                )
                """,
                owner=schema, 
                table_name=table_name.upper()
            )
            
            relationship_info = {}
            for direction, column, ref_table, ref_column in relationships:
                if ref_table not in relationship_info:
                    relationship_info[ref_table] = []
                relationship_info[ref_table].append({
                    "local_column": column,
                    "foreign_column": ref_column,
                    "direction": direction
                })
                
            return {
                "columns": column_info,
                "relationships": relationship_info
            }
            
        except oracledb.Error as e:
            print(f"Error loading table details for {table_name}: {str(e)}", file=sys.stderr)
            raise
        finally:
            await self._close_connection(conn)
    
    async def get_pl_sql_objects(self, object_type: str, name_pattern: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get PL/SQL objects"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            
            where_clause = "WHERE owner = :owner AND object_type = :object_type"
            params = {"owner": schema, "object_type": object_type}
            
            if name_pattern:
                where_clause += " AND object_name LIKE :name_pattern"
                params["name_pattern"] = name_pattern.upper()
            
            objects = await self._execute_cursor(cursor, f"""
                SELECT object_name, object_type, status, created, last_ddl_time
                FROM all_objects
                {where_clause}
                ORDER BY object_name
            """, **params)
            
            result = []
            
            for name, obj_type, status, created, last_modified in objects:
                obj_info = {
                    "name": name,
                    "type": obj_type,
                    "status": status,
                    "owner": schema
                }
                
                if created:
                    obj_info["created"] = created.strftime("%Y-%m-%d %H:%M:%S")
                if last_modified:
                    obj_info["last_modified"] = last_modified.strftime("%Y-%m-%d %H:%M:%S")
                
                result.append(obj_info)
            
            return result
        finally:
            await self._close_connection(conn)
    
    async def get_object_source(self, object_type: str, object_name: str) -> str:
        """Get the source code for a PL/SQL object"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            
            # Handle different object types accordingly
            if object_type in ('PACKAGE', 'PACKAGE BODY', 'TYPE', 'TYPE BODY'):
                # For packages and types, we need to get the full source
                source_lines = await self._execute_cursor(cursor, """
                    SELECT text
                    FROM all_source
                    WHERE owner = :owner 
                    AND name = :name 
                    AND type = :type
                    ORDER BY line
                """, owner=schema, name=object_name, type=object_type)
                
                if not source_lines:
                    return ""
                
                return "\n".join(line[0] for line in source_lines)
            else:
                # For procedures, functions, triggers, views, etc.
                result = await self._execute_cursor(cursor, """
                    SELECT dbms_metadata.get_ddl(
                        :object_type, 
                        :object_name, 
                        :owner
                    ) FROM dual
                """, 
                object_type=object_type, 
                object_name=object_name,
                owner=schema)
                
                if not result or not result[0]:
                    return ""
                    
                # Properly await the CLOB read operation
                clob = result[0][0]
                return await clob.read()
                
        except oracledb.Error as e:
            print(f"Error getting object source: {str(e)}", file=sys.stderr)
            return f"Error retrieving source: {str(e)}"
        finally:
            await self._close_connection(conn)
    
    async def get_table_constraints(self, table_name: str) -> List[Dict[str, Any]]:
        """Get table constraints"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            
            # Get all constraints for the table
            constraints = await self._execute_cursor(cursor, """
                SELECT ac.constraint_name,
                       ac.constraint_type,
                       ac.search_condition
                FROM all_constraints ac
                WHERE ac.owner = :owner
                AND ac.table_name = :table_name
            """, owner=schema, table_name=table_name.upper())
            
            result = []
            
            for constraint_name, constraint_type, condition in constraints:
                # Map constraint type codes to descriptions
                type_map = {
                    'P': 'PRIMARY KEY',
                    'R': 'FOREIGN KEY',
                    'U': 'UNIQUE',
                    'C': 'CHECK'
                }
                
                constraint_info = {
                    "name": constraint_name,
                    "type": type_map.get(constraint_type, constraint_type)
                }
                
                # Get columns involved in this constraint
                columns = await self._execute_cursor(cursor, """
                    SELECT column_name
                    FROM all_cons_columns
                    WHERE owner = :owner
                    AND constraint_name = :constraint_name
                    ORDER BY position
                """, owner=schema, constraint_name=constraint_name)
                
                constraint_info["columns"] = [col[0] for col in columns]
                
                # If it's a foreign key, get the referenced table/columns
                if constraint_type == 'R':
                    ref_info = await self._execute_cursor(cursor, """
                        SELECT ac.table_name,
                               acc.column_name
                        FROM all_constraints ac
                        JOIN all_cons_columns acc ON ac.constraint_name = acc.constraint_name
                        WHERE ac.constraint_name = (
                            SELECT r_constraint_name
                            FROM all_constraints
                            WHERE owner = :owner
                            AND constraint_name = :constraint_name
                        )
                        AND acc.owner = ac.owner
                        ORDER BY acc.position
                    """, owner=schema, constraint_name=constraint_name)
                    
                    if ref_info:
                        constraint_info["references"] = {
                            "table": ref_info[0][0],
                            "columns": [col[1] for col in ref_info]
                        }
                
                # For check constraints, include the condition
                if constraint_type == 'C' and condition:
                    constraint_info["condition"] = condition
                
                result.append(constraint_info)
            
            return result
        finally:
            await self._close_connection(conn)
    
    async def get_table_indexes(self, table_name: str) -> List[Dict[str, Any]]:
        """Get table indexes"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            
            # Get all indexes for the table
            indexes = await self._execute_cursor(cursor, """
                SELECT ai.index_name,
                       ai.uniqueness,
                       ai.tablespace_name,
                       ai.status
                FROM all_indexes ai
                WHERE ai.owner = :owner
                AND ai.table_name = :table_name
            """, owner=schema, table_name=table_name.upper())
            
            result = []
            
            for index_name, uniqueness, tablespace, status in indexes:
                index_info = {
                    "name": index_name,
                    "unique": uniqueness == 'UNIQUE'
                }
                
                if tablespace:
                    index_info["tablespace"] = tablespace
                
                if status:
                    index_info["status"] = status
                
                # Get columns in this index
                columns = await self._execute_cursor(cursor, """
                    SELECT column_name
                    FROM all_ind_columns
                    WHERE index_owner = :owner
                    AND index_name = :index_name
                    ORDER BY column_position
                """, owner=schema, index_name=index_name)
                
                index_info["columns"] = [col[0] for col in columns]
                
                result.append(index_info)
            
            return result
        finally:
            await self._close_connection(conn)
    
    async def get_dependent_objects(self, object_name: str) -> List[Dict[str, Any]]:
        """Get objects that depend on the specified object"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            
            dependencies = await self._execute_cursor(cursor, """
                WITH deps AS (
                    SELECT /*+ MATERIALIZE */
                           name, type, owner
                    FROM all_dependencies
                    WHERE referenced_name = :object_name
                    AND referenced_owner = :owner
                )
                SELECT /*+ LEADING(deps) USE_NL(ao) INDEX(ao) */
                    ao.object_name, ao.object_type, ao.owner
                FROM deps
                JOIN all_objects ao ON deps.name = ao.object_name 
                                   AND deps.type = ao.object_type
                                   AND deps.owner = ao.owner
            """, object_name=object_name, owner=schema)
            
            result = []
            
            for name, obj_type, owner in dependencies:
                result.append({
                    "name": name,
                    "type": obj_type,
                    "owner": owner
                })
            
            return result
        except oracledb.Error as e:
            print(f"Error getting dependent objects: {str(e)}", file=sys.stderr)
            raise
        finally:
            await self._close_connection(conn)
    
    async def get_user_defined_types(self, type_pattern: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get user-defined types"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            
            where_clause = "WHERE owner = :owner"
            params = {"owner": schema}
            
            if type_pattern:
                where_clause += " AND type_name LIKE :type_pattern"
                params["type_pattern"] = type_pattern.upper()
            
            types = await self._execute_cursor(cursor, f"""
                SELECT type_name, typecode
                FROM all_types
                {where_clause}
                ORDER BY type_name
            """, **params)
            
            result = []
            
            for type_name, typecode in types:
                type_info = {
                    "name": type_name,
                    "type_category": typecode,
                    "owner": schema
                }
                
                # For object types, get attributes
                if (typecode == 'OBJECT'):
                    attrs = await self._execute_cursor(cursor, """
                        SELECT attr_name, attr_type_name
                        FROM all_type_attrs
                        WHERE owner = :owner
                        AND type_name = :type_name
                        ORDER BY attr_no
                    """, owner=schema, type_name=type_name)
                    
                    if attrs:
                        type_info["attributes"] = [
                            {"name": attr[0], "type": attr[1]} for attr in attrs
                        ]
                
                result.append(type_info)
            
            return result
        finally:
            await self._close_connection(conn)
    
    async def get_related_tables(self, table_name: str) -> Dict[str, List[str]]:
        """Get all tables that are related to the specified table through foreign keys."""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            
            # Get tables referenced by this table
            referenced_tables_result = await self._execute_cursor(cursor, """
                SELECT /*+ RESULT_CACHE LEADING(ac acc) USE_NL(acc) */
                    DISTINCT acc.table_name AS referenced_table
                FROM all_constraints ac
                JOIN all_cons_columns acc ON acc.constraint_name = ac.r_constraint_name
                    AND acc.owner = ac.owner
                WHERE ac.constraint_type = 'R'
                AND ac.table_name = :table_name
                AND ac.owner = :owner
            """, table_name=table_name.upper(), owner=schema)
            
            referenced_tables = [row[0] for row in referenced_tables_result]
            
            # Get tables that reference this table
            referencing_tables_result = await self._execute_cursor(cursor, """
                WITH pk_constraints AS (
                    SELECT /*+ MATERIALIZE */ constraint_name
                    FROM all_constraints
                    WHERE table_name = :table_name
                    AND constraint_type IN ('P', 'U')
                    AND owner = :owner
                )
                SELECT /*+ RESULT_CACHE LEADING(ac pk) USE_NL(pk) */
                    DISTINCT ac.table_name AS referencing_table
                FROM pk_constraints
                JOIN pk_constraints pk ON ac.r_constraint_name = pk.constraint_name
                WHERE ac.constraint_type = 'R'
                AND ac.owner = :owner
            """, table_name=table_name.upper(), owner=schema)
            
            referencing_tables = [row[0] for row in referencing_tables_result]
            
            return {
                'referenced_tables': referenced_tables,
                'referencing_tables': referencing_tables
            }
            
        finally:
            await self._close_connection(conn)
    
    async def search_in_database(self, search_term: str, limit: int = 20) -> List[str]:
        """Search for table names in the database using similarity matching"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            # Use Oracle's built-in similarity features
            results = await self._execute_cursor(cursor, """
                SELECT /*+ RESULT_CACHE */ DISTINCT table_name 
                FROM all_tables 
                WHERE owner = :owner
                AND (
                    -- Direct matches first
                    UPPER(table_name) LIKE '%' || :search_term || '%'
                    -- Then similar names using built-in similarity calculation
                    OR UTL_MATCH.EDIT_DISTANCE_SIMILARITY(
                        UPPER(table_name),
                        :search_term
                    ) > 65  -- Minimum similarity threshold (65%)
                )
                ORDER BY 
                    CASE 
                        WHEN UPPER(table_name) LIKE '%' || :search_term || '%' THEN 0
                        ELSE 1
                    END,
                    UTL_MATCH.EDIT_DISTANCE_SIMILARITY(
                        UPPER(table_name),
                        :search_term
                    ) DESC
            """, owner=schema, search_term=search_term.upper())
            
            return [row[0] for row in results][:limit]
            
        finally:
            await self._close_connection(conn)
            
    async def search_columns_in_database(self, table_names: List[str], search_term: str) -> Dict[str, List[Dict[str, Any]]]:
        """Search for columns in specified tables"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)
            result = {}
            
            # Get columns for the specified tables that match the search term
            rows = await self._execute_cursor(cursor, """
                SELECT /*+ RESULT_CACHE */ 
                    table_name,
                    column_name,
                    data_type,
                    nullable
                FROM all_tab_columns 
                WHERE owner = :owner
                AND table_name IN (SELECT column_value FROM TABLE(CAST(:table_names AS SYS.ODCIVARCHAR2LIST)))
                AND UPPER(column_name) LIKE '%' || :search_term || '%'
                ORDER BY table_name, column_id
            """, owner=schema, 
                table_names=table_names,
                search_term=search_term.upper())
            
            for table_name, column_name, data_type, nullable in rows:
                if table_name not in result:
                    result[table_name] = []
                result[table_name].append({
                    "name": column_name,
                    "type": data_type,
                    "nullable": nullable == 'Y'
                })
            
            return result
            
        finally:
            await self._close_connection(conn)
    
    async def explain_query_plan(self, query: str) -> Dict[str, Any]:
        """Get execution plan for a SQL query"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            
            # First create an explain plan
            plan_statement = f"EXPLAIN PLAN FOR {query}"
            await cursor.execute(plan_statement)
            
            # Then retrieve the execution plan with cost and cardinality information
            plan_rows = await self._execute_cursor(cursor, """
                SELECT 
                    LPAD(' ', 2*LEVEL-2) || operation || ' ' || 
                    options || ' ' || object_name || 
                    CASE 
                        WHEN cost IS NOT NULL THEN ' (Cost: ' || cost || ')'
                        ELSE ''
                    END || 
                    CASE 
                        WHEN cardinality IS NOT NULL THEN ' (Rows: ' || cardinality || ')'
                        ELSE ''
                    END as execution_plan_step
                FROM plan_table
                START WITH id = 0
                CONNECT BY PRIOR id = parent_id
                ORDER SIBLINGS BY position
            """)
            
            # Clear the plan table for next time
            await self._execute_cursor_no_fetch(cursor,"DELETE FROM plan_table")
            await self._commit(conn)
            
            # Also get some basic optimization hints based on query content
            basic_analysis = self._analyze_query_for_optimization(query)
            
            return {
                "execution_plan": [row[0] for row in plan_rows],
                "optimization_suggestions": basic_analysis
            }
        except oracledb.Error as e:
            print(f"Error explaining query: {str(e)}", file=sys.stderr)
            return {
                "execution_plan": [],
                "optimization_suggestions": ["Unable to generate execution plan due to error."],
                "error": str(e)
            }
        finally:
            await self._close_connection(conn)
            
    def _analyze_query_for_optimization(self, query: str) -> List[str]:
        """Simple heuristic analysis of query for basic optimization suggestions"""
        query = query.upper()
        suggestions = []
        
        # Check for common inefficient patterns
        if "SELECT *" in query:
            suggestions.append("Consider selecting only needed columns instead of SELECT *")
            
        if " LIKE '%something" in query or " LIKE '%something%'" in query:
            suggestions.append("Leading wildcards in LIKE predicates prevent index usage")
            
        if " IN (SELECT " in query and " EXISTS" not in query:
            suggestions.append("Consider using EXISTS instead of IN with subqueries for better performance")
            
        if " OR " in query:
            suggestions.append("OR conditions may prevent index usage. Consider UNION ALL of separated queries")
            
        if "/*+ " not in query and len(query) > 500:
            suggestions.append("Complex query could benefit from optimizer hints")
            
        if " JOIN " in query:
            if "/*+ LEADING" not in query and query.count("JOIN") > 2:
                suggestions.append("Multi-table joins may benefit from LEADING hint to control join order")
            
            if "/*+ USE_NL" not in query and "/*+ USE_HASH" not in query and query.count("JOIN") > 1:
                suggestions.append("Consider join method hints like USE_NL or USE_HASH for complex joins")
        
        # Count number of tables and joins
        join_count = query.count(" JOIN ")
        from_count = query.count(" FROM ")
        table_count = max(from_count, join_count + 1)
        
        if table_count > 4:
            suggestions.append(f"Query joins {table_count} tables - consider reviewing join order and conditions")
            
        return suggestions

    async def get_rows_from_table(self, table_name: str, condition: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get rows from a table that match the specified condition"""
        conn = await self.get_connection()
        try:
            cursor = conn.cursor()
            schema = await self._get_effective_schema(conn)

            # Get column names for this table to format results
            columns_result = await self._execute_cursor(
                cursor,
                """
                SELECT column_name
                FROM all_tab_columns
                WHERE owner = :owner AND table_name = :table_name
                ORDER BY column_id
                """,
                owner=schema,
                table_name=table_name.upper()
            )

            column_names = [col[0] for col in columns_result]

            # Construct and execute the query
            query = f"SELECT * FROM {schema}.{table_name} WHERE {condition}"
            if limit > 0:
                query += f" FETCH FIRST {limit} ROWS ONLY"

            results = await self._execute_cursor(cursor, query)

            # Convert to list of dictionaries with column names
            formatted_results = []
            for row in results:
                row_dict = {}
                for i, col_name in enumerate(column_names):
                    # Handle None values and format data appropriately
                    if i < len(row):
                        row_dict[col_name] = row[i]
                formatted_results.append(row_dict)

            return formatted_results
        finally:
            await self._close_connection(conn)
            
    async def _close_connection(self, conn):
        """Helper method to close connection based on mode"""
        try:
            if self.thick_mode:
                conn.close()  # Synchronous close for thick mode
            else:
                await conn.close()  # Async close for thin mode
        except Exception as e:
            print(f"Error closing connection: {str(e)}", file=sys.stderr)