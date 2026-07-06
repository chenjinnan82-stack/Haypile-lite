import asyncio
import os
from app.core.exceptions import ResourceExhaustedError


class ConcurrencyLimiter:
    """
    轻量级并发限流器（支持惰性初始化以兼容跨线程/Qt UI线程实例化）
    """

    def __init__(self, max_concurrent: int, acquire_timeout_seconds: float = 0.1):
        self.max_concurrent = max(1, int(max_concurrent))
        self.acquire_timeout_seconds = acquire_timeout_seconds
        self._semaphore: asyncio.Semaphore | None = None  # 惰性加载占位

    @classmethod
    def from_cpu_count(cls, acquire_timeout_seconds: float = 0.1):
        cpu_count = os.cpu_count() or 4
        # 预留1个核心给系统UI和其他协程
        max_concurrent = max(2, cpu_count - 1)
        return cls(
            max_concurrent=max_concurrent,
            acquire_timeout_seconds=acquire_timeout_seconds,
        )

    async def acquire(self):
        # 核心防御：惰性初始化！
        # 确保 Semaphore 是在真正执行的 asyncio 事件循环中被创建，而不是在 Qt 主线程
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self.acquire_timeout_seconds
            )
        except asyncio.TimeoutError:
            raise ResourceExhaustedError("系统繁忙，已达到并发上限，触发限流熔断")

    def release(self):
        if self._semaphore is not None:
            self._semaphore.release()

    # 提供 Async Context Manager 支持 (async with ...)
    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()
