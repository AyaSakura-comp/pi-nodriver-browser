"""Provider-attempt accounting; reservation is the dispatch boundary."""
import asyncio
from .contracts import identifier, natural


class QueryBudget:
    def __init__(self, limit: int):
        natural(limit, 'limit')
        self.limit = limit
        self._attempts: dict[str, None] = {}
        self._lock = asyncio.Lock()

    @property
    def used(self) -> int:
        return len(self._attempts)

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    @property
    def attempts(self) -> tuple[str, ...]:
        return tuple(self._attempts)

    async def reserve(self, attempt_id: str) -> bool:
        """No refunds after dispatch. Dedup/cancellation checks precede this call."""
        identifier(attempt_id)
        async with self._lock:
            if attempt_id in self._attempts or self.remaining <= 0:
                return False
            self._attempts[attempt_id] = None
            return True
