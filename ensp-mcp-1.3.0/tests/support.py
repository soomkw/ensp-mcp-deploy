import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class ScriptedReader:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, size):
        if not self.chunks:
            await asyncio.sleep(3600)
        item = self.chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Writer:
    def __init__(self, on_write=None):
        self.sent = []
        self.closed = False
        self.on_write = on_write

    def is_closing(self):
        return self.closed

    def write(self, data):
        self.sent.append(data)
        if self.on_write:
            self.on_write(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakePool:
    def __init__(self, device):
        self.device = device
        self.kwargs = None

    def lock_for(self, *args):
        return asyncio.Lock()

    async def acquire(self, *args, **kwargs):
        self.kwargs = kwargs
        return self.device
