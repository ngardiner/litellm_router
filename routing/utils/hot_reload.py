"""
Module hot-reloading system for runtime code updates.
"""

import os
import sys
import time
import logging
import importlib
import threading
from typing import Dict, Set, Callable, Optional
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent
from .module_loader import get_module_loader
from ..database.schema import register_module

logger = logging.getLogger(__name__)


class ModuleFileHandler(FileSystemEventHandler):
    """Handler for module file system events."""
    
    def __init__(self, hot_reloader: "HotReloader"):
        self.hot_reloader = hot_reloader
    
    def on_modified(self, event):
        """Handle file modification events."""
        if not isinstance(event, FileModifiedEvent):
            return
        
        if event.src_path.endswith(".py") and not event.src_path.endswith(
            "__pycache__"
        ):
            module_path = Path(event.src_path)
            if module_path.parent.name == "modules":
                module_name = module_path.stem
                if not module_name.startswith("__"):
                    logger.info(f"Module file changed: {module_name}")
                    self.hot_reloader.queue_reload(module_name)


class HotReloader:
    """Module hot-reloading system."""
    
    def __init__(self, modules_directory: Optional[str] = None):
        if modules_directory is None:
            # Default to the modules directory
            current_dir = Path(__file__).parent.parent
            modules_directory = current_dir / "modules"
        
        self.modules_directory = Path(modules_directory)
        self.observer = None
        self.reload_queue = set()
        self.reload_lock = threading.Lock()
        self.reload_callbacks: Dict[str, Set[Callable]] = {}
        self.module_versions: Dict[str, int] = {}
        self.enabled = False
        
        # Debounce settings
        self.reload_debounce_seconds = 1.0
        self.last_reload_times: Dict[str, float] = {}
        
        # Background reload processor
        self.reload_thread = None
        self.stop_event = threading.Event()
    
    def start_watching(self) -> None:
        """Start watching for module file changes."""
        if not self.modules_directory.exists():
            logger.warning(
                f"Modules directory does not exist: {self.modules_directory}"
            )
            return
        
        if self.observer is not None:
            logger.warning("Hot reloader is already watching")
            return
        
        try:
            self.observer = Observer()
            handler = ModuleFileHandler(self)
            self.observer.schedule(handler, str(self.modules_directory), recursive=False)
            self.observer.start()
            
            # Start reload processor thread
            self.reload_thread = threading.Thread(target=self._process_reload_queue, daemon=True)
            self.reload_thread.start()
            
            self.enabled = True
            logger.info(f"Hot reloader started watching: {self.modules_directory}")
            
        except Exception as e:
            logger.error(f"Failed to start hot reloader: {e}")
    
    def stop_watching(self) -> None:
        """Stop watching for module file changes."""
        self.enabled = False
        
        if self.observer is not None:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        
        if self.reload_thread is not None:
            self.stop_event.set()
            self.reload_thread.join(timeout=5)
            self.reload_thread = None
        
        logger.info("Hot reloader stopped")
    
    def queue_reload(self, module_name: str) -> None:
        """Queue a module for reloading with debouncing."""
        current_time = time.time()
        
        # Check debounce
        last_reload = self.last_reload_times.get(module_name, 0)
        if current_time - last_reload < self.reload_debounce_seconds:
            logger.debug(f"Debouncing reload for {module_name}")
            return
        
        with self.reload_lock:
            self.reload_queue.add(module_name)
            self.last_reload_times[module_name] = current_time
        
        logger.debug(f"Queued module for reload: {module_name}")
    
    def _process_reload_queue(self) -> None:
        """Background thread to process reload queue."""
        while not self.stop_event.is_set():
            try:
                modules_to_reload = set()
                
                with self.reload_lock:
                    if self.reload_queue:
                        modules_to_reload = self.reload_queue.copy()
                        self.reload_queue.clear()
                
                for module_name in modules_to_reload:
                    try:
                        self._reload_module(module_name)
                    except Exception as e:
                        logger.error(f"Failed to reload module {module_name}: {e}")
                
                # Sleep before checking queue again
                self.stop_event.wait(0.5)
                
            except Exception as e:
                logger.error(f"Error in reload queue processor: {e}")
                self.stop_event.wait(1.0)
    
    def _reload_module(self, module_name: str) -> bool:
        """Reload a specific module."""
        try:
            logger.info(f"Hot-reloading module: {module_name}")
            
            # Get the module loader
            module_loader = get_module_loader()
            
            # Check if module file exists
            module_path = self.modules_directory / f"{module_name}.py"
            if not module_path.exists():
                logger.error(f"Module file not found: {module_path}")
                return False
            
            # Validate syntax before reloading
            if not self._validate_module_syntax(module_path):
                logger.error(f"Module {module_name} has syntax errors, skipping reload")
                return False
            
            # Clear module from sys.modules if it exists
            module_full_name = f"routing.modules.{module_name}"
            if module_full_name in sys.modules:
                del sys.modules[module_full_name]
            
            # Force reload in module loader
            success = module_loader.reload_module(module_name)
            if not success:
                logger.error(f"Failed to reload module {module_name}")
                return False
            
            # Validate the reloaded module
            if not module_loader.validate_module_interface(module_name):
                logger.error(f"Reloaded module {module_name} failed interface validation")
                return False
            
            # Update module version
            self.module_versions[module_name] = self.module_versions.get(module_name, 0) + 1
            
            # Re-register module in database with updated metadata
            try:
                module = module_loader.load_module(module_name)
                if module:
                    display_name = getattr(module, "MODULE_NAME", module_name.title())
                    description = getattr(
                        module, "MODULE_DESCRIPTION", f"Routing module: {module_name}"
                    )
                    version = getattr(
                        module, "MODULE_VERSION", f"1.0.{self.module_versions[module_name]}"
                    )
                    config_schema = getattr(module, "CONFIG_SCHEMA", None)
                    
                    register_module(module_name, display_name, description, config_schema, version)
                    logger.info(f"Re-registered module {module_name} in database")
            except Exception as e:
                logger.warning(f"Failed to re-register module {module_name} in database: {e}")
            
            # Notify callbacks
            self._notify_reload_callbacks(module_name)
            
            logger.info(f"Successfully hot-reloaded module: {module_name} (version {self.module_versions[module_name]})")
            return True
            
        except Exception as e:
            logger.error(f"Failed to hot-reload module {module_name}: {e}")
            return False
    
    def _validate_module_syntax(self, module_path: Path) -> bool:
        """Validate module syntax before reloading."""
        try:
            with open(module_path, "r", encoding="utf-8") as f:
                source_code = f.read()
            
            # Compile to check syntax
            compile(source_code, str(module_path), "exec")
            return True
            
        except SyntaxError as e:
            logger.error(f"Syntax error in {module_path}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error validating {module_path}: {e}")
            return False
    
    def _notify_reload_callbacks(self, module_name: str) -> None:
        """Notify registered callbacks about module reload."""
        callbacks = self.reload_callbacks.get(module_name, set())
        for callback in callbacks:
            try:
                callback(module_name, self.module_versions.get(module_name, 0))
            except Exception as e:
                logger.error(f"Error in reload callback for {module_name}: {e}")
    
    def register_reload_callback(self, module_name: str, callback: Callable[[str, int], None]) -> None:
        """Register a callback to be called when a module is reloaded."""
        if module_name not in self.reload_callbacks:
            self.reload_callbacks[module_name] = set()
        self.reload_callbacks[module_name].add(callback)
        logger.debug(f"Registered reload callback for {module_name}")
    
    def unregister_reload_callback(self, module_name: str, callback: Callable[[str, int], None]) -> None:
        """Unregister a reload callback."""
        if module_name in self.reload_callbacks:
            self.reload_callbacks[module_name].discard(callback)
            if not self.reload_callbacks[module_name]:
                del self.reload_callbacks[module_name]
    
    def manual_reload(self, module_name: str) -> bool:
        """Manually trigger a module reload."""
        logger.info(f"Manual reload requested for module: {module_name}")
        return self._reload_module(module_name)
    
    def get_module_version(self, module_name: str) -> int:
        """Get the current version number of a module."""
        return self.module_versions.get(module_name, 0)
    
    def get_status(self) -> Dict[str, any]:
        """Get hot reloader status."""
        return {
            "enabled": self.enabled,
            "watching": self.observer is not None and self.observer.is_alive(),
            "modules_directory": str(self.modules_directory),
            "reload_queue_size": len(self.reload_queue),
            "module_versions": self.module_versions.copy(),
            "registered_callbacks": {
                module: len(callbacks)
                for module, callbacks in self.reload_callbacks.items()
            },
        }


# Global hot reloader instance
_hot_reloader = None


def get_hot_reloader() -> HotReloader:
    """Get the global hot reloader instance."""
    global _hot_reloader
    if _hot_reloader is None:
        _hot_reloader = HotReloader()
    return _hot_reloader


def start_hot_reloading() -> None:
    """Start the hot reloading system."""
    try:
        hot_reloader = get_hot_reloader()
        hot_reloader.start_watching()
        logger.info("Module hot-reloading started")
    except Exception as e:
        logger.error(f"Failed to start hot-reloading: {e}")


def stop_hot_reloading() -> None:
    """Stop the hot reloading system."""
    try:
        hot_reloader = get_hot_reloader()
        hot_reloader.stop_watching()
        logger.info("Module hot-reloading stopped")
    except Exception as e:
        logger.error(f"Failed to stop hot-reloading: {e}")


def manual_reload_module(module_name: str) -> bool:
    """Manually reload a specific module."""
    hot_reloader = get_hot_reloader()
    return hot_reloader.manual_reload(module_name)