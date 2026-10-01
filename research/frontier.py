"""Single-owner BFS frontier, with barriers covering search AND decisions."""
from collections import deque
from .contracts import SearchTask, natural


class Frontier:
    def __init__(self):
        self._pending: dict[int, deque[SearchTask]] = {}
        self._running: dict[str, SearchTask] = {}
        self._tasks: dict[str, SearchTask] = {}
        self._topics: dict[str, tuple[int, str | None]] = {}
        self._queries: set[tuple[str, str]] = set()

    @property
    def pending_count(self) -> int:
        return sum(map(len, self._pending.values()))

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def idle(self) -> bool:
        return not self.pending_count and not self._running

    def add(self, task: SearchTask) -> bool:
        if task.task_id in self._tasks:
            raise ValueError('task ID already used')
        if task.parent_topic_id is not None:
            parent = self._topics.get(task.parent_topic_id)
            if parent is None or task.depth != parent[0] + 1:
                raise ValueError('unknown parent or invalid child depth')
        identity = (task.depth, task.parent_topic_id)
        if task.topic_id in self._topics and self._topics[task.topic_id] != identity:
            raise ValueError('topic ID reused with a different lineage')
        if task.query_key in self._queries:
            return False
        self._queries.add(task.query_key)
        self._tasks[task.task_id] = task
        self._topics[task.topic_id] = identity
        self._pending.setdefault(task.depth, deque()).append(task)
        return True

    def take(self, count: int) -> list[SearchTask]:
        natural(count, 'count')
        if not count or not self.pending_count:
            return []
        depth = min(d for d, q in self._pending.items() if q)
        active_depths = {t.depth for t in self._running.values()}
        # A late shallow discovery becomes a barrier, without cancelling running work.
        if active_depths and active_depths != {depth}:
            return []
        out = []
        q = self._pending[depth]
        while q and len(out) < count:
            task = q.popleft()
            self._running[task.task_id] = task
            out.append(task)
        return out

    def complete(self, task_id: str) -> None:
        if task_id not in self._running:
            raise ValueError('task is not running')
        del self._running[task_id]
