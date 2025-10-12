"""
Unit tests for hot reload functionality.
"""

import pytest
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from routing.utils.hot_reload import HotReloader, get_hot_reloader


class TestHotReloader:
    """Test hot reload functionality."""
    
    def test_hot_reloader_creation(self):
        """Test creating a hot reloader."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            assert reloader.modules_directory == Path(temp_dir)
            assert not reloader.enabled
            assert reloader.observer is None
    
    def test_queue_reload_with_debouncing(self):
        """Test queuing reloads with debouncing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            reloader.reload_debounce_seconds = 0.1
            
            # First reload should be queued
            reloader.queue_reload("test_module")
            assert "test_module" in reloader.reload_queue
            
            # Immediate second reload should be debounced
            reloader.queue_reload("test_module")
            assert len(reloader.reload_queue) == 1
            
            # After debounce period, should queue again
            time.sleep(0.2)
            reloader.queue_reload("test_module")
            # Should still be only one in queue (same module)
            assert len(reloader.reload_queue) == 1
    
    @patch('routing.utils.hot_reload.get_module_loader')
    def test_validate_module_syntax_valid(self, mock_get_loader):
        """Test validating module with valid syntax."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            
            # Create a valid Python file
            module_file = Path(temp_dir) / "test_module.py"
            module_file.write_text("""
def route(model, messages, config, **kwargs):
    return "test-result"

MODULE_NAME = "Test Module"
""")
            
            assert reloader._validate_module_syntax(module_file) == True
    
    def test_validate_module_syntax_invalid(self):
        """Test validating module with invalid syntax."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            
            # Create an invalid Python file
            module_file = Path(temp_dir) / "test_module.py"
            module_file.write_text("""
def route(model, messages, config, **kwargs
    return "test-result"  # Missing closing parenthesis
""")
            
            assert reloader._validate_module_syntax(module_file) == False
    
    @patch('routing.utils.hot_reload.get_module_loader')
    @patch('routing.utils.hot_reload.register_module')
    def test_reload_module_success(self, mock_register, mock_get_loader):
        """Test successful module reload."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            
            # Create a valid Python file
            module_file = Path(temp_dir) / "test_module.py"
            module_file.write_text("""
def route(model, messages, config, **kwargs):
    return "test-result"

MODULE_NAME = "Test Module"
MODULE_DESCRIPTION = "Test Description"
MODULE_VERSION = "1.0.0"
""")
            
            # Mock module loader
            mock_loader = Mock()
            mock_loader.reload_module.return_value = True
            mock_loader.validate_module_interface.return_value = True
            mock_loader.load_module.return_value = Mock(
                MODULE_NAME="Test Module",
                MODULE_DESCRIPTION="Test Description", 
                MODULE_VERSION="1.0.0"
            )
            mock_get_loader.return_value = mock_loader
            
            # Test reload
            success = reloader._reload_module("test_module")
            
            assert success == True
            assert reloader.module_versions["test_module"] == 1
            mock_loader.reload_module.assert_called_once_with("test_module")
            mock_register.assert_called_once()
    
    @patch('routing.utils.hot_reload.get_module_loader')
    def test_reload_module_missing_file(self, mock_get_loader):
        """Test reload when module file is missing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            
            # Test reload of non-existent file
            success = reloader._reload_module("nonexistent_module")
            
            assert success == False
    
    @patch('routing.utils.hot_reload.get_module_loader')
    def test_reload_module_validation_failure(self, mock_get_loader):
        """Test reload when module validation fails."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            
            # Create a valid Python file
            module_file = Path(temp_dir) / "test_module.py"
            module_file.write_text("""
def route(model, messages, config, **kwargs):
    return "test-result"
""")
            
            # Mock module loader to fail validation
            mock_loader = Mock()
            mock_loader.reload_module.return_value = True
            mock_loader.validate_module_interface.return_value = False
            mock_get_loader.return_value = mock_loader
            
            # Test reload
            success = reloader._reload_module("test_module")
            
            assert success == False
    
    def test_reload_callbacks(self):
        """Test reload callback system."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            
            # Create a callback
            callback_called = []
            def test_callback(module_name, version):
                callback_called.append((module_name, version))
            
            # Register callback
            reloader.register_reload_callback("test_module", test_callback)
            assert "test_module" in reloader.reload_callbacks
            
            # Notify callbacks
            reloader.module_versions["test_module"] = 5
            reloader._notify_reload_callbacks("test_module")
            
            assert len(callback_called) == 1
            assert callback_called[0] == ("test_module", 5)
            
            # Unregister callback
            reloader.unregister_reload_callback("test_module", test_callback)
            assert "test_module" not in reloader.reload_callbacks
    
    def test_get_status(self):
        """Test getting hot reloader status."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            reloader.module_versions["test_module"] = 3
            
            status = reloader.get_status()
            
            assert status["enabled"] == False
            assert status["watching"] == False
            assert status["modules_directory"] == temp_dir
            assert status["reload_queue_size"] == 0
            assert status["module_versions"]["test_module"] == 3
    
    @patch('routing.utils.hot_reload.get_module_loader')
    @patch('routing.utils.hot_reload.register_module')
    def test_manual_reload(self, mock_register, mock_get_loader):
        """Test manual module reload."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            
            # Create a valid Python file
            module_file = Path(temp_dir) / "test_module.py"
            module_file.write_text("""
def route(model, messages, config, **kwargs):
    return "test-result"

MODULE_NAME = "Test Module"
""")
            
            # Mock module loader
            mock_loader = Mock()
            mock_loader.reload_module.return_value = True
            mock_loader.validate_module_interface.return_value = True
            mock_loader.load_module.return_value = Mock(MODULE_NAME="Test Module")
            mock_get_loader.return_value = mock_loader
            
            # Test manual reload
            success = reloader.manual_reload("test_module")
            
            assert success == True
            mock_loader.reload_module.assert_called_once_with("test_module")
    
    def test_module_versions(self):
        """Test module version tracking."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reloader = HotReloader(temp_dir)
            
            # Initially no version
            assert reloader.get_module_version("test_module") == 0
            
            # Set version
            reloader.module_versions["test_module"] = 5
            assert reloader.get_module_version("test_module") == 5


class TestHotReloadGlobalFunctions:
    """Test global hot reload functions."""
    
    def test_get_hot_reloader_singleton(self):
        """Test that get_hot_reloader returns singleton."""
        reloader1 = get_hot_reloader()
        reloader2 = get_hot_reloader()
        assert reloader1 is reloader2
    
    @patch('routing.utils.hot_reload.get_hot_reloader')
    def test_start_hot_reloading(self, mock_get_reloader):
        """Test starting hot reloading."""
        from routing.utils.hot_reload import start_hot_reloading
        
        mock_reloader = Mock()
        mock_get_reloader.return_value = mock_reloader
        
        start_hot_reloading()
        
        mock_reloader.start_watching.assert_called_once()
    
    @patch('routing.utils.hot_reload.get_hot_reloader')
    def test_stop_hot_reloading(self, mock_get_reloader):
        """Test stopping hot reloading."""
        from routing.utils.hot_reload import stop_hot_reloading
        
        mock_reloader = Mock()
        mock_get_reloader.return_value = mock_reloader
        
        stop_hot_reloading()
        
        mock_reloader.stop_watching.assert_called_once()
    
    @patch('routing.utils.hot_reload.get_hot_reloader')
    def test_manual_reload_module(self, mock_get_reloader):
        """Test manual module reload function."""
        from routing.utils.hot_reload import manual_reload_module
        
        mock_reloader = Mock()
        mock_reloader.manual_reload.return_value = True
        mock_get_reloader.return_value = mock_reloader
        
        success = manual_reload_module("test_module")
        
        assert success == True
        mock_reloader.manual_reload.assert_called_once_with("test_module")