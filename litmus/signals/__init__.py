"""预置事件库：事件模板与参数取值范围，加载时逐条用 expr 校验（ARCHITECTURE §4.3）。

其他模块只从这里 import（§1.2 第 3 条）。
"""

from litmus.signals.library import (
    EventDefinition,
    EventDefinitionError,
    EventLibrary,
    EventParamError,
    ParamSpec,
    RenderedEvent,
    event_catalog,
    load_events,
    parse_library,
    render_event,
)

__all__ = [
    "EventDefinition",
    "EventDefinitionError",
    "EventLibrary",
    "EventParamError",
    "ParamSpec",
    "RenderedEvent",
    "event_catalog",
    "load_events",
    "parse_library",
    "render_event",
]
