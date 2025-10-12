"""
Integration tests for the web interface.
"""

import pytest
import httpx
import asyncio
from unittest.mock import patch, MagicMock
import sys
import os

# Add the web interface to the path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "web_interface"))

# Skip tests if FastAPI is not available
pytest = pytest
fastapi_available = True
try:
    import fastapi
    from app import app
except ImportError:
    fastapi_available = False
    app = None


@pytest.fixture
def client():
    """Create a test client for the FastAPI app."""
    if not fastapi_available:
        pytest.skip("FastAPI not available")
    return httpx.AsyncClient(app=app, base_url="http://test")


@pytest.fixture
def mock_db():
    """Mock database connections for testing."""
    with patch('routing.database.connection.get_simple_connection') as mock_conn:
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.return_value = mock_connection
        yield mock_connection, mock_cursor


@pytest.mark.skipif(not fastapi_available, reason="FastAPI not available")
class TestHealthEndpoint:
    """Test health check endpoint."""
    
    @pytest.mark.asyncio
    async def test_health_endpoint_success(self, client, mock_db):
        """Test successful health check."""
        mock_connection, mock_cursor = mock_db
        
        with patch('routing.router.health_check') as mock_health:
            mock_health.return_value = {
                "status": "healthy",
                "database": "connected",
                "modules": {"openrouter_free": "valid"},
                "routing_rules_count": 1,
                "errors": []
            }
            
            response = await client.get("/api/health")
            
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert data["database"] == "connected"
    
    @pytest.mark.asyncio
    async def test_health_endpoint_failure(self, client):
        """Test health check with error."""
        with patch('routing.router.health_check') as mock_health:
            mock_health.side_effect = Exception("Database error")
            
            response = await client.get("/api/health")
            
            assert response.status_code == 500


@pytest.mark.skipif(not fastapi_available, reason="FastAPI not available")
class TestRoutingRulesAPI:
    """Test routing rules API endpoints."""
    
    @pytest.mark.asyncio
    async def test_get_routing_rules(self, client, mock_db):
        """Test getting routing rules."""
        mock_connection, mock_cursor = mock_db
        
        with patch('routing.database.schema.get_routing_rules') as mock_get_rules:
            mock_get_rules.return_value = [
                ("glm-4.5-air", "openrouter_free", True, 0)
            ]
            
            response = await client.get("/api/routing-rules")
            
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["model_name"] == "glm-4.5-air"
            assert data[0]["module_name"] == "openrouter_free"
    
    @pytest.mark.asyncio
    async def test_create_routing_rule(self, client, mock_db):
        """Test creating a routing rule."""
        mock_connection, mock_cursor = mock_db
        
        rule_data = {
            "model_name": "test-model",
            "module_name": "test_module",
            "enabled": True,
            "priority": 5
        }
        
        response = await client.post("/api/routing-rules", json=rule_data)
        
        assert response.status_code == 200
        data = response.json()
        assert "successfully" in data["message"]
        
        # Verify database was called
        mock_cursor.execute.assert_called_once()
        mock_connection.commit.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_delete_routing_rule(self, client, mock_db):
        """Test deleting a routing rule."""
        mock_connection, mock_cursor = mock_db
        mock_cursor.rowcount = 1  # Simulate successful deletion
        
        response = await client.delete("/api/routing-rules/test-model")
        
        assert response.status_code == 200
        data = response.json()
        assert "deleted successfully" in data["message"]
    
    @pytest.mark.asyncio
    async def test_delete_routing_rule_not_found(self, client, mock_db):
        """Test deleting a non-existent routing rule."""
        mock_connection, mock_cursor = mock_db
        mock_cursor.rowcount = 0  # Simulate no rows affected
        
        response = await client.delete("/api/routing-rules/nonexistent")
        
        assert response.status_code == 404


@pytest.mark.skipif(not fastapi_available, reason="FastAPI not available")
class TestModulesAPI:
    """Test modules API endpoints."""
    
    @pytest.mark.asyncio
    async def test_get_modules(self, client, mock_db):
        """Test getting modules."""
        mock_connection, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = [
            ("openrouter_free", "OpenRouter Free", "Free tier routing", "1.0.0", True, {"test": "schema"})
        ]
        
        response = await client.get("/api/modules")
        
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["module_name"] == "openrouter_free"
        assert data[0]["display_name"] == "OpenRouter Free"
    
    @pytest.mark.asyncio
    async def test_discover_modules(self, client):
        """Test module discovery."""
        with patch('routing.router.auto_discover_and_register_modules') as mock_discover:
            response = await client.post("/api/modules/discover")
            
            assert response.status_code == 200
            mock_discover.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_module_config(self, client):
        """Test getting module configuration."""
        with patch('routing.database.schema.get_module_config') as mock_get_config:
            mock_get_config.return_value = {
                "cooldown_reset_hour": "0",
                "fallback_enabled": "true"
            }
            
            response = await client.get("/api/modules/openrouter_free/config")
            
            assert response.status_code == 200
            data = response.json()
            assert data["cooldown_reset_hour"] == "0"
            assert data["fallback_enabled"] == "true"
    
    @pytest.mark.asyncio
    async def test_set_module_config(self, client):
        """Test setting module configuration."""
        with patch('routing.database.schema.set_module_config') as mock_set_config:
            config_data = {
                "cooldown_reset_hour": "2",
                "fallback_enabled": "false"
            }
            
            response = await client.post("/api/modules/test_module/config", json=config_data)
            
            assert response.status_code == 200
            assert mock_set_config.call_count == 2  # Called for each config item


@pytest.mark.skipif(not fastapi_available, reason="FastAPI not available")
class TestCooldownsAPI:
    """Test cooldowns API endpoints."""
    
    @pytest.mark.asyncio
    async def test_get_cooldowns(self, client, mock_db):
        """Test getting cooldowns."""
        from datetime import datetime, timezone
        
        mock_connection, mock_cursor = mock_db
        future_time = datetime.now(timezone.utc)
        mock_cursor.fetchall.return_value = [
            ("test-model-free", future_time)
        ]
        
        response = await client.get("/api/cooldowns")
        
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["model_key"] == "test-model-free"
    
    @pytest.mark.asyncio
    async def test_clear_cooldown(self, client, mock_db):
        """Test clearing a cooldown."""
        mock_connection, mock_cursor = mock_db
        mock_cursor.rowcount = 1  # Simulate successful deletion
        
        response = await client.delete("/api/cooldowns/test-model-free")
        
        assert response.status_code == 200
        data = response.json()
        assert "cleared" in data["message"]


@pytest.mark.skipif(not fastapi_available, reason="FastAPI not available")
class TestStatsAPI:
    """Test statistics API endpoint."""
    
    @pytest.mark.asyncio
    async def test_get_stats(self, client, mock_db):
        """Test getting platform statistics."""
        mock_connection, mock_cursor = mock_db
        
        # Mock multiple database queries
        mock_cursor.fetchone.side_effect = [
            (2,),  # active rules
            (3,),  # total rules
            (1,),  # active modules
            (2,),  # total modules
            (0,),  # active cooldowns
        ]
        
        with patch('routing.database.schema.get_current_schema_version') as mock_version:
            mock_version.return_value = 1
            
            response = await client.get("/api/stats")
            
            assert response.status_code == 200
            data = response.json()
            assert data["routing_rules"]["active"] == 2
            assert data["routing_rules"]["total"] == 3
            assert data["modules"]["active"] == 1
            assert data["modules"]["total"] == 2
            assert data["cooldowns"]["active"] == 0
            assert data["schema_version"] == 1


@pytest.mark.skipif(not fastapi_available, reason="FastAPI not available")
class TestRootEndpoint:
    """Test the root HTML endpoint."""
    
    @pytest.mark.asyncio
    async def test_root_endpoint(self, client):
        """Test the root HTML page."""
        response = await client.get("/")
        
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "LiteLLM Router Platform" in response.text