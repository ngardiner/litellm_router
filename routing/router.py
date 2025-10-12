"""
Main switchboard router for LiteLLM routing platform.
"""

import logging
from typing import Optional, List, Dict, Any
from .database.schema import get_routing_rules, get_module_config
from .utils.module_loader import get_module_loader

logger = logging.getLogger(__name__)


def switchboard(model: str, messages: list, **kwargs) -> Optional[str]:
    """
    Main switchboard function for routing decisions.
    
    This function is called by LiteLLM for each request and determines
    which model to route to based on database-stored routing rules.
    
    Args:
        model: The requested model name
        messages: The conversation messages
        **kwargs: Additional LiteLLM parameters
    
    Returns:
        str: Target model name to route to
        None: No routing decision, fall back to default LiteLLM behavior
    """
    logger.info(f"Switchboard received request for model: {model}")
    
    try:
        # Get routing rules from database
        routing_rules = get_routing_rules()
        
        # Find matching rule for the requested model
        matching_rule = None
        for rule_model, rule_module, enabled, priority in routing_rules:
            if rule_model == model and enabled:
                matching_rule = (rule_module, priority)
                break
        
        if matching_rule is None:
            logger.info(f"No routing rule found for model '{model}'. Using default LiteLLM behavior.")
            return None
        
        module_name, priority = matching_rule
        logger.info(f"Found routing rule: {model} -> {module_name} (priority: {priority})")
        
        # Load module configuration
        module_config = get_module_config(module_name)
        logger.debug(f"Module config for {module_name}: {module_config}")
        
        # Get module loader and call the routing function
        module_loader = get_module_loader()
        result = module_loader.call_module_route(
            module_name, model, messages, module_config, **kwargs
        )
        
        if result is not None:
            logger.info(f"Module {module_name} routed {model} to {result}")
            return result
        else:
            logger.info(f"Module {module_name} returned None, using default LiteLLM behavior")
            return None
    
    except Exception as e:
        logger.error(f"Error in switchboard for model {model}: {e}")
        # On any error, fall back to default LiteLLM behavior
        return None


def register_routing_rule(model_name: str, module_name: str, priority: int = 0) -> None:
    """
    Register a routing rule in the database.
    
    Args:
        model_name: The model name to route
        module_name: The module to handle routing
        priority: Rule priority (higher = more important)
    """
    from .database.connection import get_simple_connection
    
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO routing_rules (model_name, module_name, priority, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (model_name) 
                DO UPDATE SET 
                    module_name = EXCLUDED.module_name,
                    priority = EXCLUDED.priority,
                    updated_at = NOW()
                """,
                (model_name, module_name, priority)
            )
            conn.commit()
    
    logger.info(f"Registered routing rule: {model_name} -> {module_name} (priority: {priority})")


def auto_discover_and_register_modules() -> None:
    """
    Automatically discover available modules and register them in the database.
    """
    from .database.schema import register_module
    
    module_loader = get_module_loader()
    discovered_modules = module_loader.discover_modules()
    
    for module_name in discovered_modules:
        if module_loader.validate_module_interface(module_name):
            try:
                module = module_loader.load_module(module_name)
                if module:
                    # Get module metadata
                    display_name = getattr(module, 'MODULE_NAME', module_name.title())
                    description = getattr(module, 'MODULE_DESCRIPTION', f'Routing module: {module_name}')
                    version = getattr(module, 'MODULE_VERSION', '1.0.0')
                    config_schema = getattr(module, 'CONFIG_SCHEMA', None)
                    
                    # Register in database
                    register_module(module_name, display_name, description, config_schema, version)
                    logger.info(f"Registered module: {module_name}")
            except Exception as e:
                logger.error(f"Failed to register module {module_name}: {e}")
        else:
            logger.warning(f"Module {module_name} failed interface validation")


def health_check() -> Dict[str, Any]:
    """
    Perform health check of the routing system.
    
    Returns:
        dict: Health check results
    """
    results = {
        "status": "healthy",
        "database": "unknown",
        "modules": {},
        "errors": []
    }
    
    try:
        # Check database connectivity
        routing_rules = get_routing_rules()
        results["database"] = "connected"
        results["routing_rules_count"] = len(routing_rules)
    except Exception as e:
        results["database"] = "error"
        results["errors"].append(f"Database error: {e}")
        results["status"] = "unhealthy"
    
    try:
        # Check module loading
        module_loader = get_module_loader()
        discovered_modules = module_loader.discover_modules()
        
        for module_name in discovered_modules:
            try:
                is_valid = module_loader.validate_module_interface(module_name)
                results["modules"][module_name] = "valid" if is_valid else "invalid"
                if not is_valid:
                    results["errors"].append(f"Module {module_name} failed validation")
            except Exception as e:
                results["modules"][module_name] = "error"
                results["errors"].append(f"Module {module_name} error: {e}")
    
    except Exception as e:
        results["errors"].append(f"Module discovery error: {e}")
        results["status"] = "unhealthy"
    
    if results["errors"]:
        results["status"] = "unhealthy"
    
    return results