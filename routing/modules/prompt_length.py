"""
Prompt length routing module.

Routes requests to different model tiers based on the total character length
of the input messages.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module metadata
MODULE_NAME = "Prompt Length Router"
MODULE_DESCRIPTION = "Routes requests based on the total character length of the messages"
MODULE_VERSION = "1.0.0"

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "threshold": {
            "type": "integer",
            "default": 1000,
            "description": "Character length threshold for switching models"
        },
        "model_under": {
            "type": "string",
            "description": "Model to use when length is under threshold"
        },
        "model_over": {
            "type": "string",
            "description": "Model to use when length is over threshold"
        }
    },
    "required": ["model_under", "model_over"]
}


def route(model: str, messages: list, config: dict, **kwargs) -> Optional[str]:
    """
    Route requests based on prompt length.

    Args:
        model: The requested model name
        messages: The conversation messages
        config: Module configuration from database
        **kwargs: Additional LiteLLM parameters

    Returns:
        str: Target model name
        None: No routing decision
    """
    threshold = int(config.get("threshold", 1000))
    model_under = config.get("model_under")
    model_over = config.get("model_over")

    if not model_under or not model_over:
        logger.warning("Prompt length module misconfigured: model_under or model_over missing")
        return None

    total_length = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            total_length += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_length += len(part.get("text", ""))

    logger.info(f"Prompt length: {total_length} (threshold: {threshold})")

    if total_length <= threshold:
        logger.info(f"Length under threshold. Routing to {model_under}")
        return model_under
    else:
        logger.info(f"Length over threshold. Routing to {model_over}")
        return model_over
