from __future__ import annotations

import inspect
from typing import Any, Callable, Optional
import uuid


def govern_owui_tools(
    owui_tools: dict[str, Any],
    *,
    middleware: Any,
    base_context: Any,
    event_call: Optional[Callable],
    metadata: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Wrap all request-scoped OWUI callables before any adapter executes them."""
    governed: dict[str, Any] = {}
    for exposed_name, entry in owui_tools.items():
        if not isinstance(entry, dict):
            continue
        original_callable = entry.get("callable")
        is_direct = bool(entry.get("direct"))
        if not callable(original_callable) and not is_direct:
            governed[exposed_name] = entry
            continue

        def _make_original(
            tool_name: str,
            tool_entry: dict[str, Any],
            tool_callable: Optional[Callable],
            direct: bool,
        ) -> Callable:
            async def _original(**params: Any) -> Any:
                if direct:
                    if event_call is None:
                        raise RuntimeError(
                            "This browser-side Open WebUI tool requires an active WebSocket session."
                        )
                    return await event_call(
                        {
                            "type": "execute:tool",
                            "data": {
                                "id": str(uuid.uuid4()),
                                "name": tool_name,
                                "params": params,
                                "server": tool_entry.get("server", {}),
                                "session_id": (metadata or {}).get("session_id"),
                            },
                        }
                    )
                result = tool_callable(**params)  # type: ignore[misc]
                if inspect.isawaitable(result):
                    result = await result
                return result

            return _original

        original = _make_original(
            str(exposed_name), entry, original_callable, is_direct
        )

        def _make_governed(tool_name: str, callable_: Callable) -> Callable:
            async def _governed(**params: Any) -> Any:
                return await middleware.execute(
                    tool_name=tool_name,
                    arguments=params,
                    original_callable=callable_,
                    context=base_context.for_tool(tool_name),
                )

            return _governed

        wrapped = dict(entry)
        wrapped["callable"] = _make_governed(str(exposed_name), original)
        wrapped["direct"] = False
        governed[exposed_name] = wrapped
    return governed
