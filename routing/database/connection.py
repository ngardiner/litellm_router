"""
Database connection management utilities.
"""

import os
import psycopg2
from psycopg2 import pool
from typing import Optional
from contextlib import contextmanager

from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

def clean_database_url(url: str) -> str:
    if not url:
        return url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs.pop("connection_limit", None)
    qs.pop("pgbouncer", None)
    new_query = urlencode(qs, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))



class DatabaseConnection:
    """Database connection manager with connection pooling."""
    
    _pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
    
    @classmethod
    def initialize_pool(cls, min_conn: int = 1, max_conn: int = 20) -> None:
        """Initialize the connection pool."""
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL environment variable is required")
        
        cls._pool = psycopg2.pool.ThreadedConnectionPool(
            min_conn, max_conn, clean_database_url(database_url)
        )
    
    @classmethod
    def get_connection(cls):
        """Get a connection from the pool."""
        if cls._pool is None:
            cls.initialize_pool()
        return cls._pool.getconn()
    
    @classmethod
    def return_connection(cls, conn) -> None:
        """Return a connection to the pool."""
        if cls._pool:
            cls._pool.putconn(conn)
    
    @classmethod
    def close_all_connections(cls) -> None:
        """Close all connections in the pool."""
        if cls._pool:
            cls._pool.closeall()
            cls._pool = None


@contextmanager
def get_db_connection():
    """Context manager for database connections."""
    conn = None
    try:
        conn = DatabaseConnection.get_connection()
        yield conn
    finally:
        if conn:
            DatabaseConnection.return_connection(conn)


def get_simple_connection():
    """Get a simple database connection (for migration scripts)."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required")
    return psycopg2.connect(clean_database_url(database_url))