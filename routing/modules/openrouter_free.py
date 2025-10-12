"""
OpenRouter Free routing module.

Implements transparent fallback from free to paid model tiers with intelligent
cooldown management to avoid repeated free model failures.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from ..database.connection import get_simple_connection

logger = logging.getLogger(__name__)

# Module metadata
MODULE_NAME = "OpenRouter Free Fallback"
MODULE_DESCRIPTION = "Transparent fallback from free to paid model tiers with cooldown management"
MODULE_VERSION = "1.0.0"

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "cooldown_reset_hour": {
            "type": "integer",
            "minimum": 0,
            "maximum": 23,
            "default": 0,
            "description": "Hour of day to reset cooldowns (0-23)"
        },
        "cooldown_timezone": {
            "type": "string",
            "default": "UTC",
            "description": "Timezone for cooldown reset calculation"
        },
        "fallback_enabled": {
            "type": "boolean",
            "default": True,
            "description": "Whether to enable paid fallback when free model is on cooldown"
        }
    }
}


def route(model: str, messages: list, config: dict, **kwargs) -> Optional[str]:
    """
    Route requests between free and paid model tiers.
    
    Args:
        model: The requested model name
        messages: The conversation messages
        config: Module configuration from database
        **kwargs: Additional LiteLLM parameters
    
    Returns:
        str: Target model name ("{model}-free" or "{model}-paid")
        None: No routing decision, fall back to default
    """
    logger.info(f"OpenRouter Free module processing model: {model}")
    
    # Extract configuration with defaults
    fallback_enabled = config.get("fallback_enabled", "true").lower() == "true"
    
    # Check if free model is on cooldown
    free_model_key = f"{model}-free"
    is_on_cooldown = check_cooldown(free_model_key)
    
    if is_on_cooldown:
        if fallback_enabled:
            paid_model = f"{model}-paid"
            logger.info(f"Free model {free_model_key} is on cooldown. Routing to {paid_model}")
            return paid_model
        else:
            logger.info(f"Free model {free_model_key} is on cooldown but fallback disabled")
            return None
    else:
        free_model = f"{model}-free"
        logger.info(f"Free model {free_model} is available. Routing to free tier")
        return free_model


def check_cooldown(model_key: str) -> bool:
    """
    Check if a model is currently on cooldown.
    
    Args:
        model_key: The model key to check
    
    Returns:
        bool: True if model is on cooldown, False otherwise
    """
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cooldown_until FROM model_cooldowns 
                    WHERE model_key = %s AND cooldown_until > NOW()
                    """,
                    (model_key,)
                )
                result = cursor.fetchone()
                is_on_cooldown = result is not None
                
                if is_on_cooldown:
                    logger.debug(f"Model {model_key} is on cooldown until {result[0]}")
                else:
                    logger.debug(f"Model {model_key} is not on cooldown")
                
                return is_on_cooldown
                
    except Exception as e:
        logger.error(f"Error checking cooldown for {model_key}: {e}")
        # On error, assume not on cooldown to allow requests
        return False


def set_cooldown(model_key: str, cooldown_until: datetime) -> None:
    """
    Set a cooldown for a specific model.
    
    Args:
        model_key: The model key to set cooldown for
        cooldown_until: When the cooldown expires
    """
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO model_cooldowns (model_key, cooldown_until, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (model_key) 
                    DO UPDATE SET 
                        cooldown_until = EXCLUDED.cooldown_until,
                        updated_at = NOW()
                    """,
                    (model_key, cooldown_until)
                )
                conn.commit()
                
        logger.info(f"Set cooldown for {model_key} until {cooldown_until}")
        
    except Exception as e:
        logger.error(f"Error setting cooldown for {model_key}: {e}")


def calculate_next_reset_time(reset_hour: int = 0, timezone_str: str = "UTC") -> datetime:
    """
    Calculate the next reset time based on configuration.
    
    Args:
        reset_hour: Hour of day to reset (0-23)
        timezone_str: Timezone name
    
    Returns:
        datetime: Next reset time with timezone
    """
    try:
        tz = timezone.utc if timezone_str == "UTC" else timezone.utc  # Simplified for now
        now = datetime.now(tz)
        
        # Calculate next reset time
        next_reset = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        
        # If the reset time has already passed today, move to tomorrow
        if next_reset <= now:
            next_reset += timedelta(days=1)
        
        return next_reset
        
    except Exception as e:
        logger.error(f"Error calculating reset time: {e}")
        # Fallback to next midnight UTC
        now_utc = datetime.now(timezone.utc)
        return (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def handle_rate_limit_error(model_name: str, config: dict) -> None:
    """
    Handle rate limit error by setting appropriate cooldown.
    
    This function should be called by LiteLLM failure callback when
    a rate limit error occurs on a free model.
    
    Args:
        model_name: The model that hit rate limit
        config: Module configuration
    """
    logger.warning(f"Rate limit error detected for model: {model_name}")
    
    # Extract configuration
    reset_hour = int(config.get("cooldown_reset_hour", "0"))
    timezone_str = config.get("cooldown_timezone", "UTC")
    
    # Calculate cooldown expiry
    cooldown_until = calculate_next_reset_time(reset_hour, timezone_str)
    
    # Set cooldown
    set_cooldown(model_name, cooldown_until)
    
    logger.info(f"Set cooldown for {model_name} until {cooldown_until}")


def clear_expired_cooldowns() -> int:
    """
    Clear expired cooldowns from the database.
    
    Returns:
        int: Number of cooldowns cleared
    """
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM model_cooldowns WHERE cooldown_until <= NOW()"
                )
                cleared_count = cursor.rowcount
                conn.commit()
                
        logger.info(f"Cleared {cleared_count} expired cooldowns")
        return cleared_count
        
    except Exception as e:
        logger.error(f"Error clearing expired cooldowns: {e}")
        return 0


def get_cooldown_status() -> dict:
    """
    Get status of all current cooldowns.
    
    Returns:
        dict: Cooldown status information
    """
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT model_key, cooldown_until 
                    FROM model_cooldowns 
                    WHERE cooldown_until > NOW()
                    ORDER BY cooldown_until
                    """
                )
                active_cooldowns = {row[0]: row[1].isoformat() for row in cursor.fetchall()}
                
                cursor.execute("SELECT COUNT(*) FROM model_cooldowns")
                total_cooldowns = cursor.fetchone()[0]
                
        return {
            "active_cooldowns": active_cooldowns,
            "total_cooldown_records": total_cooldowns,
            "active_count": len(active_cooldowns)
        }
        
    except Exception as e:
        logger.error(f"Error getting cooldown status: {e}")
        return {"error": str(e)}