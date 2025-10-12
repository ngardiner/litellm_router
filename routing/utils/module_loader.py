"""
Dynamic module loading and discovery utilities.
"""

import os
import importlib
import importlib.util
from typing import Dict, List, Callable, Any, Optional
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ModuleLoader:
    """Dynamic module loader for routing modules."""
    
    def __init__(self, modules_directory: str = None):
        if modules_directory is None:
            # Default to the modules directory relative to this file
            current_dir = Path(__file__).parent.parent
            modules_directory = current_dir / "modules"
        
        self.modules_directory = Path(modules_directory)
        self._loaded_modules: Dict[str, Any] = {}
        self._module_configs: Dict[str, dict] = {}
    
    def discover_modules(self) -> List[str]:
        """Discover all available routing modules."""
        if not self.modules_directory.exists():
            logger.warning(f"Modules directory does not exist: {self.modules_directory}")
            return []
        
        modules = []
        for file_path in self.modules_directory.glob("*.py"):
            if file_path.name.startswith("__"):
                continue
            
            module_name = file_path.stem
            modules.append(module_name)
        
        logger.info(f"Discovered modules: {modules}")
        return modules
    
    def load_module(self, module_name: str) -> Optional[Any]:
        """Load a specific routing module."""
        if module_name in self._loaded_modules:
            return self._loaded_modules[module_name]
        
        module_path = self.modules_directory / f"{module_name}.py"
        if not module_path.exists():
            logger.error(f"Module file not found: {module_path}")
            return None
        
        try:
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            if spec is None or spec.loader is None:
                logger.error(f"Failed to create module spec for {module_name}")
                return None
            
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            # Validate module has required interface
            if not hasattr(module, 'route'):
                logger.error(f"Module {module_name} missing required 'route' function")
                return None
            
            self._loaded_modules[module_name] = module
            logger.info(f"Successfully loaded module: {module_name}")
            return module
            
        except Exception as e:
            logger.error(f"Failed to load module {module_name}: {e}")
            return None
    
    def get_module_route_function(self, module_name: str) -> Optional[Callable]:
        """Get the route function from a specific module."""
        module = self.load_module(module_name)
        if module is None:
            return None
        
        return getattr(module, 'route', None)
    
    def call_module_route(self, module_name: str, model: str, messages: list, 
                         config: dict, **kwargs) -> Optional[str]:
        """Call the route function of a specific module."""
        route_func = self.get_module_route_function(module_name)
        if route_func is None:
            logger.error(f"No route function found for module: {module_name}")
            return None
        
        try:
            result = route_func(model, messages, config, **kwargs)
            logger.debug(f"Module {module_name} returned: {result}")
            return result
        except Exception as e:
            logger.error(f"Error calling route function in module {module_name}: {e}")
            return None
    
    def reload_module(self, module_name: str) -> bool:
        """Reload a specific module (for hot-reloading)."""
        if module_name in self._loaded_modules:
            del self._loaded_modules[module_name]
        
        return self.load_module(module_name) is not None
    
    def get_loaded_modules(self) -> List[str]:
        """Get list of currently loaded modules."""
        return list(self._loaded_modules.keys())
    
    def validate_module_interface(self, module_name: str) -> bool:
        """Validate that a module implements the required interface."""
        module = self.load_module(module_name)
        if module is None:
            return False
        
        # Check for required route function
        if not hasattr(module, 'route'):
            return False
        
        route_func = getattr(module, 'route')
        if not callable(route_func):
            return False
        
        # Optional: Check for metadata attributes
        metadata_attrs = ['MODULE_NAME', 'MODULE_DESCRIPTION', 'MODULE_VERSION']
        for attr in metadata_attrs:
            if hasattr(module, attr):
                logger.debug(f"Module {module_name} has {attr}: {getattr(module, attr)}")
        
        return True


# Global module loader instance
_module_loader = None


def get_module_loader() -> ModuleLoader:
    """Get the global module loader instance."""
    global _module_loader
    if _module_loader is None:
        _module_loader = ModuleLoader()
    return _module_loader