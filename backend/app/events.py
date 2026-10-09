import logging
from collections import deque
from app.timezones import beijing_now
from threading import Lock
from app.ids import ShortId

from app.contracts import RunEvent

logger = logging.getLogger("zhixing.events")


class InfrastructureEvents:
    """仅保存当前进程基础设施事件；不是 M02 永久业务审计。"""

    def __init__(self, capacity: int = 100) -> None:
        """参数 capacity 控制内存事件上限；初始化线程锁和序列号，无返回值。"""
        # 当前进程的有界事件窗口，防止健康轮询无限占用内存。
        self._events: deque[RunEvent] = deque(maxlen=capacity)
        self._sequence = 0
        self._lock = Lock()

    def record(self, trace_id: ShortId, event_type: str, data: dict) -> RunEvent:
        """接收追踪 ID、事件类型及安全数据；追加递增事件并返回事件对象。"""
        with self._lock:
            self._sequence += 1
            event = RunEvent(event_id=self._sequence, trace_id=trace_id,
                             timestamp=beijing_now(), type=event_type, data=data)
            self._events.append(event)
        logger.info(event.model_dump_json())
        return event

    def read(self, after: int, limit: int) -> list[RunEvent]:
        """接收序号下界和条数上限；返回窗口内大于下界的事件，按序排列。"""
        with self._lock:
            return [event for event in self._events if event.event_id > after][:limit]
