import asyncio
import contextlib

from .async_utils import infinite_loop, retry, synchronizer, asynccontextmanager, TaskContext
from .client import Client
from .config import logger
from .ctx_mgr_utils import CtxMgr
from .grpc_utils import BLOCKING_REQUEST_TIMEOUT, GRPC_REQUEST_TIME_BUFFER, ChannelPool
from .image import base_image
from .object import Object
from .proto import api_pb2
from .utils import print_logs


@synchronizer
class Session(Object):
    def __init__(self):
        self._functions = []
        super().__init__()

    async def create_or_get(self, obj, tag=None, return_copy=False):
        if return_copy:
            # Don't modify the underlying object, just return a joined object
            cls = type(obj)
            new_obj = cls.__new__(cls)
            new_obj.args = obj.args
            obj = new_obj

        obj.session = self
        obj.tag = tag
        obj.client = self.client
        obj.object_id = await obj._create_or_get()
        obj.created  = True
        return obj

    def function(self, raw_f, image=base_image):
        fun = image.function(raw_f)
        self._functions.append(fun)
        return fun

    async def _get_logs(self, draining=False, timeout=BLOCKING_REQUEST_TIMEOUT):
        request = api_pb2.SessionGetLogsRequest(session_id=self.session_id, timeout=timeout, draining=draining)
        async for log_entry in self.client.stub.SessionGetLogs(request, timeout=timeout + GRPC_REQUEST_TIME_BUFFER):
            if log_entry.done:
                logger.info("No more logs")
                break
            else:
                print_logs(log_entry.data, log_entry.fd, self._stdout, self._stderr)

    @asynccontextmanager
    async def run(self, client=None):
        if client is None:
            client = await Client.current()

        self.client = client

        # Get all objects on this session right now
        objects = {tag: getattr(self, tag)
                   for tag in dir(self)
                   if isinstance(getattr(self, tag), Object)}

        # Add all functions (TODO: this is super dumb)
        objects |= {f"fun_{i}": fun for i, fun in enumerate(self._functions)}

        # Start session
        # TODO: pass in a list of tags that need to be pre-created
        req = api_pb2.SessionCreateRequest(client_id=client.client_id)
        resp = await client.stub.SessionCreate(req)
        self.session_id = resp.session_id

        # Create all members
        # TODO: do this in parallel
        for tag, obj in objects.items():
            await self.create_or_get(obj, tag)

        # Start tracking logs and yield context
        async with TaskContext() as tc:
            tc.create_task(infinite_loop(self._get_logs))
            yield

        # Stop session (this causes the server to kill any running task)
        logger.debug("Stopping the session server-side")
        req = api_pb2.SessionStopRequest(session_id=self.session_id)
        await self.client.stub.SessionStop(req)

        # Fetch any straggling logs
        logger.debug("Draining logs")
        await self._get_logs(draining=True, timeout=10.0)
