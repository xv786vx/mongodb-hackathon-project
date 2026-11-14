"""One database connection per event loop; no cached identity, prompts or conversation history."""

import asyncio
import os
import weakref
from dataclasses import dataclass

from pymongo import AsyncMongoClient
from mystery_core.memory.config import Settings
from mystery_core.memory.store import MemoryStore
from mystery_core.memory.traces import TraceStore
from mystery_core.engine.turn import TurnService


@dataclass
class Runtime:
    store: MemoryStore
    traces: TraceStore
    turns: TurnService
    client: AsyncMongoClient

    @classmethod
    async def open(cls):
        settings = Settings.from_env()
        if settings.embedding_model != "voyage-4-lite" or settings.embedding_dims != 1024:
            raise ValueError("suspect runtime requires voyage-4-lite at 1024 dimensions")
        model = os.environ.get("LLM_MODEL", "").strip()
        if not model:
            raise ValueError("LLM_MODEL is required")
        client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True, appname="halloway-suspect",
                                  serverSelectionTimeoutMS=15000)
        try:
            store = await MemoryStore.open(settings, client=client)
            traces = TraceStore(client[settings.db_name])
            return cls(store, traces, TurnService(store, traces, model=model), client)
        except BaseException:
            await client.close()
            raise


_runtimes = weakref.WeakKeyDictionary()


async def get_runtime() -> Runtime:
    loop = asyncio.get_running_loop()
    task = _runtimes.get(loop)
    if task is None:
        task = loop.create_task(Runtime.open())
        _runtimes[loop] = task
    try:
        return await asyncio.shield(task)
    except BaseException:
        if task.done() and (task.cancelled() or task.exception() is not None):
            _runtimes.pop(loop, None)
        raise
