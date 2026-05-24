"""
Callback dispatcher — routes LiteLLM CustomLogger hooks to any module that implements them.

Registered as a single LiteLLM callback alongside pii_hasher. For each hook that fires,
it discovers enabled modules in this directory and calls the matching async function if
the module implements it. pii_hasher is excluded (registered as a direct callback).

Adding a new hook module:
    1. Create routing/modules/my_module.py in the litellm_edge modules directory.
    2. Implement any subset of the hook functions documented below (all optional, all async).
    3. Set ROUTING_MODULE = False (or omit; dispatcher accepts either type).
    4. The dispatcher picks it up automatically within one cache refresh cycle (60s).

Hook functions a module can implement:
    async_moderation_hook(data, user_api_key_dict, call_type)
        Called for every request before it is sent to the LLM.
        Return modified data dict to transform, raise HTTPException to block the request.

    async_filter_deployments(model, healthy_deployments, messages, request_kwargs)
        Called when LiteLLM is selecting a deployment from its model group.
        Return a filtered/reordered subset of healthy_deployments.

    async_get_chat_completion_prompt(model, messages, non_default_params, **kwargs)
        Called before the prompt is sent. Return (model, messages, non_default_params)
        tuple to transform any of these, or None to leave unchanged.

    async_pre_call_hook(user_api_key_dict, cache, data, call_type)
        Called before the completion API call. Return modified data dict or None.

    async_pre_request_hook(model, messages, kwargs)
        Called before Anthropic passthrough requests. Mutate messages in-place; return None.

    async_pre_call_deployment_hook(kwargs, call_type)
        Called before each deployment attempt. Return modified kwargs dict or None.

    async_post_call_success_deployment_hook(request_data, response, call_type)
        Called after a successful non-streaming response. Return modified response or None.

    async_post_call_streaming_deployment_hook(request_data, response_chunk, call_type)
        Called for each streaming chunk. Return modified chunk or None.

    async_post_call_success_hook(data, user_api_key_dict, response)
        Called after every successful completion. For metrics/analytics. Return value ignored.

    async_post_call_failure_hook(request_data, original_exception, user_api_key_dict)
        Called after every failed completion. For alerting. Return value ignored.

    async_post_call_response_headers_hook(data, user_api_key_dict, response, request_headers)
        Called before headers are returned to the client. Return a dict of headers to add/merge.

    async_logging_hook(kwargs, result, call_type)
        Called before request/response data is written to spend_logs.
        Return (modified_kwargs, modified_result) tuple, or None to leave unchanged.

    async_log_success_event(kwargs, response_obj, start_time, end_time)
        Low-level success event. For detailed access logging. Return value ignored.

    async_log_failure_event(kwargs, response_obj, start_time, end_time)
        Low-level failure event. For failure logging. Return value ignored.
"""

import importlib.util
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from litellm.integrations.custom_logger import CustomLogger as _CustomLogger
except ImportError:
    class _CustomLogger:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass

logger = logging.getLogger(__name__)

MODULE_NAME = "Callback Dispatcher"
MODULE_DESCRIPTION = (
    "Dispatches LiteLLM hooks to any module that implements the corresponding async function"
)
MODULE_VERSION = "1.0.0"
ROUTING_MODULE = False

# Modules registered as direct LiteLLM callbacks — skip to avoid double-dispatch
_DIRECT_CALLBACKS = frozenset({"pii_hasher", "callback_dispatcher"})

_HOOK_NAMES = frozenset({
    "async_moderation_hook",
    "async_filter_deployments",
    "async_get_chat_completion_prompt",
    "async_pre_call_hook",
    "async_pre_request_hook",
    "async_pre_call_deployment_hook",
    "async_post_call_success_hook",
    "async_post_call_success_deployment_hook",
    "async_post_call_streaming_deployment_hook",
    "async_post_call_failure_hook",
    "async_post_call_response_headers_hook",
    "async_logging_hook",
    "async_log_success_event",
    "async_log_failure_event",
})

_CACHE_TTL = 60.0  # seconds between module rediscovery


def _module_enabled(module_name: str) -> bool:
    """Check routing_modules table; default True if not registered or DB unavailable."""
    try:
        import psycopg2
        import urllib.parse
        url = os.environ["DATABASE_URL"]
        clean = urllib.parse.urlparse(url)._replace(query="").geturl()
        with psycopg2.connect(clean) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT enabled FROM routing_modules WHERE module_name = %s",
                    (module_name,),
                )
                row = cur.fetchone()
                return bool(row[0]) if row else True
    except Exception:
        return True


class RouterCallbackDispatcher(_CustomLogger):
    """
    Fan-out dispatcher. See module docstring for the full hook interface.
    """

    def __init__(self, **kwargs):
        self._modules_dir = Path(__file__).parent
        self._cache: List[Any] = []
        self._cache_at: float = 0.0
        super().__init__(**kwargs)
        logger.info("RouterCallbackDispatcher initialised (modules_dir=%s)", self._modules_dir)

    # ------------------------------------------------------------------
    # Module discovery
    # ------------------------------------------------------------------

    def _get_modules(self) -> List[Any]:
        now = time.monotonic()
        if now - self._cache_at < _CACHE_TTL:
            return self._cache

        loaded: List[Any] = []
        for path in sorted(self._modules_dir.glob("*.py")):
            name = path.stem
            if name.startswith("__") or name in _DIRECT_CALLBACKS:
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"_dispatched_{name}", path)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                if not any(hasattr(mod, h) for h in _HOOK_NAMES):
                    continue
                if not _module_enabled(name):
                    logger.debug("Dispatcher: module %s is disabled, skipping", name)
                    continue

                loaded.append(mod)
                logger.debug("Dispatcher: registered hook module %s", name)
            except Exception as exc:
                logger.warning("Dispatcher: failed to load %s: %s", name, exc)

        self._cache = loaded
        self._cache_at = now
        if loaded:
            names = [getattr(m, "MODULE_NAME", m.__name__) for m in loaded]
            logger.info("Dispatcher: active hook modules: %s", names)
        return loaded

    # ------------------------------------------------------------------
    # Transformation hooks — pipe semantics; chain result through modules
    # ------------------------------------------------------------------

    async def async_moderation_hook(self, data: dict, user_api_key_dict, call_type: str):
        """Pipe: each module can transform data or raise to block the request."""
        for mod in self._get_modules():
            fn = getattr(mod, "async_moderation_hook", None)
            if fn:
                try:
                    result = await fn(data, user_api_key_dict, call_type)
                    if result is not None:
                        data = result
                except Exception as exc:
                    # Re-raise HTTPException (these are intentional blocks)
                    from starlette.exceptions import HTTPException
                    if isinstance(exc, HTTPException):
                        raise
                    logger.error("Dispatcher: async_moderation_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return data

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list,
        messages: Optional[list],
        request_kwargs: Optional[dict] = None,
        parent_otel_span=None,
    ) -> list:
        """Pipe: each module can further filter or reorder the deployment list."""
        for mod in self._get_modules():
            fn = getattr(mod, "async_filter_deployments", None)
            if fn:
                try:
                    healthy_deployments = await fn(model, healthy_deployments, messages, request_kwargs)
                except Exception as exc:
                    logger.error("Dispatcher: async_filter_deployments error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return healthy_deployments

    async def async_get_chat_completion_prompt(
        self,
        model: str,
        messages: list,
        non_default_params: dict,
        prompt_id=None,
        prompt_variables=None,
        dynamic_callback_params=None,
        litellm_logging_obj=None,
        **kwargs,
    ) -> Tuple[str, list, dict]:
        """Pipe: each module can transform model, messages, and/or params."""
        for mod in self._get_modules():
            fn = getattr(mod, "async_get_chat_completion_prompt", None)
            if fn:
                try:
                    result = await fn(model, messages, non_default_params, **kwargs)
                    if result is not None:
                        model, messages, non_default_params = result
                except Exception as exc:
                    logger.error("Dispatcher: async_get_chat_completion_prompt error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return model, messages, non_default_params

    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type: str):
        """Pipe: each module can transform the request data dict."""
        changed = False
        for mod in self._get_modules():
            fn = getattr(mod, "async_pre_call_hook", None)
            if fn:
                try:
                    result = await fn(user_api_key_dict, cache, data, call_type)
                    if result is not None:
                        data = result
                        changed = True
                except Exception as exc:
                    logger.error("Dispatcher: async_pre_call_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return data if changed else None

    async def async_pre_request_hook(self, model: str, messages: list, kwargs: dict):
        """Broadcast: modules mutate messages in-place (Anthropic passthrough path)."""
        for mod in self._get_modules():
            fn = getattr(mod, "async_pre_request_hook", None)
            if fn:
                try:
                    await fn(model, messages, kwargs)
                except Exception as exc:
                    logger.error("Dispatcher: async_pre_request_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return None

    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type=None):
        """Pipe: each module can transform request kwargs before a deployment is tried."""
        changed = False
        for mod in self._get_modules():
            fn = getattr(mod, "async_pre_call_deployment_hook", None)
            if fn:
                try:
                    result = await fn(kwargs, call_type)
                    if result is not None:
                        kwargs = result
                        changed = True
                except Exception as exc:
                    logger.error("Dispatcher: async_pre_call_deployment_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return kwargs if changed else None

    async def async_post_call_success_deployment_hook(self, request_data: dict, response, call_type=None):
        """Pipe: each module can transform the non-streaming response."""
        changed = False
        for mod in self._get_modules():
            fn = getattr(mod, "async_post_call_success_deployment_hook", None)
            if fn:
                try:
                    result = await fn(request_data, response, call_type)
                    if result is not None:
                        response = result
                        changed = True
                except Exception as exc:
                    logger.error("Dispatcher: async_post_call_success_deployment_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return response if changed else None

    async def async_post_call_streaming_deployment_hook(self, request_data: dict, response_chunk, call_type=None):
        """Pipe: each module can transform the current streaming chunk."""
        changed = False
        for mod in self._get_modules():
            fn = getattr(mod, "async_post_call_streaming_deployment_hook", None)
            if fn:
                try:
                    result = await fn(request_data, response_chunk, call_type)
                    if result is not None:
                        response_chunk = result
                        changed = True
                except Exception as exc:
                    logger.error("Dispatcher: async_post_call_streaming_deployment_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return response_chunk if changed else None

    async def async_logging_hook(self, kwargs: dict, result: Any, call_type: str) -> Tuple[dict, Any]:
        """Pipe: each module can scrub/transform data before it is written to spend_logs."""
        for mod in self._get_modules():
            fn = getattr(mod, "async_logging_hook", None)
            if fn:
                try:
                    out = await fn(kwargs, result, call_type)
                    if out is not None:
                        kwargs, result = out
                except Exception as exc:
                    logger.error("Dispatcher: async_logging_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return kwargs, result

    async def async_post_call_response_headers_hook(
        self, data: dict, user_api_key_dict, response, request_headers=None
    ) -> Optional[dict]:
        """Merge: accumulate headers returned by all modules."""
        headers: Dict[str, str] = {}
        for mod in self._get_modules():
            fn = getattr(mod, "async_post_call_response_headers_hook", None)
            if fn:
                try:
                    result = await fn(data, user_api_key_dict, response, request_headers)
                    if result:
                        headers.update(result)
                except Exception as exc:
                    logger.error("Dispatcher: async_post_call_response_headers_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)
        return headers if headers else None

    # ------------------------------------------------------------------
    # Notification hooks — broadcast; return values ignored
    # ------------------------------------------------------------------

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict, response):
        """Broadcast: notify all modules of a successful completion."""
        for mod in self._get_modules():
            fn = getattr(mod, "async_post_call_success_hook", None)
            if fn:
                try:
                    await fn(data, user_api_key_dict, response)
                except Exception as exc:
                    logger.error("Dispatcher: async_post_call_success_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)

    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict,
        traceback_str=None,
    ):
        """Broadcast: notify all modules of a failed completion."""
        for mod in self._get_modules():
            fn = getattr(mod, "async_post_call_failure_hook", None)
            if fn:
                try:
                    await fn(request_data, original_exception, user_api_key_dict)
                except Exception as exc:
                    logger.error("Dispatcher: async_post_call_failure_hook error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)

    async def async_log_success_event(self, kwargs: dict, response_obj, start_time, end_time):
        """Broadcast: low-level success event for detailed logging modules."""
        for mod in self._get_modules():
            fn = getattr(mod, "async_log_success_event", None)
            if fn:
                try:
                    await fn(kwargs, response_obj, start_time, end_time)
                except Exception as exc:
                    logger.error("Dispatcher: async_log_success_event error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)

    async def async_log_failure_event(self, kwargs: dict, response_obj, start_time, end_time):
        """Broadcast: low-level failure event for detailed logging modules."""
        for mod in self._get_modules():
            fn = getattr(mod, "async_log_failure_event", None)
            if fn:
                try:
                    await fn(kwargs, response_obj, start_time, end_time)
                except Exception as exc:
                    logger.error("Dispatcher: async_log_failure_event error in %s: %s",
                                 getattr(mod, "MODULE_NAME", mod.__name__), exc)


# Module-level singleton — registered in config.yaml:
#   routing.modules.callback_dispatcher.dispatcher
dispatcher = RouterCallbackDispatcher()
