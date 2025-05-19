from pathlib import Path
from typing import Optional, List, Dict, Any

from .database import DatabaseConnector
from .schema.manager import SchemaManager
from .models import TableInfo


class DatabaseContext:
    def __init__(self, connection_string: str, cache_path: Path, target_schema: Optional[str] = None,  use_thick_mode: bool = False, lib_dir: Optional[str] = None):
        self.db_connector = DatabaseConnector(connection_string, target_schema, use_thick_mode, lib_dir)
        self.schema_manager = SchemaManager(self.db_connector, cache_path)
        # Set the schema manager reference in the connector
        self.db_connector.set_schema_manager(self.schema_manager)
        
    async def initialize(self) -> None:
        """Initialize the database context, connection pool, and schema cache"""
        await self.db_connector.initialize_pool()
        await self.schema_manager.initialize()
        
    async def close(self) -> None:
        """Close the database context and connection pool"""
        await self.db_connector.close_pool()
        
    async def get_database_info(self):
        """Get information about the database vendor and version"""
        return await self.db_connector.get_database_info()
        
    async def get_schema_info(self, table_name: str) -> Optional[TableInfo]:
        """Get schema information for a specific table"""
        return await self.schema_manager.get_schema_info(table_name)
    
    async def search_tables(self, search_term: str, limit: int = 20) -> List[str]:
        """Search for table names matching the search term"""
        return await self.schema_manager.search_tables(search_term, limit)
        
    async def rebuild_cache(self) -> None:
        """Force a rebuild of the schema cache"""
        self.schema_manager.cache = await self.schema_manager.load_or_build_cache(force_rebuild=True)
        
    async def search_columns(self, search_term: str, limit: int = 50) -> Dict[str, List[Dict[str, Any]]]:
        """Search for columns matching the given pattern across all tables"""
        return await self.schema_manager.search_columns(search_term, limit)
        
    async def get_pl_sql_objects(self, object_type: str, name_pattern: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get information about PL/SQL objects of the specified type"""
        # First check schema manager cache
        cache_key = f"{object_type}_{name_pattern or 'all'}"
        if self.schema_manager.is_cache_valid('plsql', cache_key):
            self.schema_manager.cache_stats['hits'] += 1
            return self.schema_manager.object_cache['plsql'][cache_key]['data']
        
        # If not in cache or expired, get from database
        self.schema_manager.cache_stats['misses'] += 1
        result = await self.db_connector.get_pl_sql_objects(object_type, name_pattern)
        
        # Update cache
        self.schema_manager.update_cache('plsql', cache_key, result)
        await self.schema_manager.save_cache()
        return result
        
    async def get_object_source(self, object_type: str, object_name: str) -> str:
        """Get the source code for a PL/SQL object"""
        return await self.db_connector.get_object_source(object_type, object_name)
        
    async def get_table_constraints(self, table_name: str) -> List[Dict[str, Any]]:
        """Get constraints for a specific table"""
        # Check cache first
        if self.schema_manager.is_cache_valid('constraints', table_name):
            self.schema_manager.cache_stats['hits'] += 1
            return self.schema_manager.object_cache['constraints'][table_name]['data']
        
        # If not in cache or expired, get from database
        self.schema_manager.cache_stats['misses'] += 1
        result = await self.db_connector.get_table_constraints(table_name)
        
        # Update cache
        self.schema_manager.update_cache('constraints', table_name, result)
        await self.schema_manager.save_cache()
        return result
        
    async def get_table_indexes(self, table_name: str) -> List[Dict[str, Any]]:
        """Get indexes for a specific table"""
        # Check cache first
        if self.schema_manager.is_cache_valid('indexes', table_name):
            self.schema_manager.cache_stats['hits'] += 1
            return self.schema_manager.object_cache['indexes'][table_name]['data']
        
        # If not in cache or expired, get from database
        self.schema_manager.cache_stats['misses'] += 1
        result = await self.db_connector.get_table_indexes(table_name)
        
        # Update cache
        self.schema_manager.update_cache('indexes', table_name, result)
        await self.schema_manager.save_cache()
        return result
        
    async def get_dependent_objects(self, object_name: str) -> List[Dict[str, Any]]:
        """Get objects that depend on the specified object"""
        return await self.db_connector.get_dependent_objects(object_name)
        
    async def get_user_defined_types(self, type_pattern: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get information about user-defined types"""
        # Check cache first
        cache_key = type_pattern or 'all'
        if self.schema_manager.is_cache_valid('types', cache_key):
            self.schema_manager.cache_stats['hits'] += 1
            return self.schema_manager.object_cache['types'][cache_key]['data']
        
        # If not in cache or expired, get from database
        self.schema_manager.cache_stats['misses'] += 1
        result = await self.db_connector.get_user_defined_types(type_pattern)
        
        # Update cache
        self.schema_manager.update_cache('types', cache_key, result)
        await self.schema_manager.save_cache()
        return result

    async def get_related_tables(self, table_name: str) -> Dict[str, List[str]]:
        """Get all tables that are related to the specified table through foreign keys."""
        # Check cache first
        cache_key = f"related_{table_name}"
        if self.schema_manager.is_cache_valid('related_tables', cache_key):
            self.schema_manager.cache_stats['hits'] += 1
            return self.schema_manager.object_cache['related_tables'][cache_key]['data']
        
        # If not in cache or expired, get from database
        self.schema_manager.cache_stats['misses'] += 1
        result = await self.db_connector.get_related_tables(table_name)
        
        # Update cache
        self.schema_manager.update_cache('related_tables', cache_key, result)
        await self.schema_manager.save_cache()
        return result

    async def explain_query_plan(self, query: str) -> Dict[str, Any]:
        """Get execution plan for an SQL query with optimization suggestions"""
        return await self.db_connector.explain_query_plan(query)

    async def execute_select_query(self, query: str) -> list:
        """Execute a SELECT query and return the results as a list of dicts."""
        return await self.db_connector.execute_select_query(query)

    async def execute_update_query(self, query: str) -> int:
        """Execute an UPDATE query and return the number of affected rows."""
        return await self.db_connector.execute_update_query(query)

    async def execute_delete_query(self, query: str) -> int:
        """Execute a DELETE query and return the number of affected rows."""
        return await self.db_connector.execute_delete_query(query)