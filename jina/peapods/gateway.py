import asyncio
import os
import threading

import grpc

from .grpc_asyncio import AsyncioExecutor
from .zmq import AsyncZmqlet, add_envelope
from .. import __stop_msg__
from ..excepts import WaitPendingMessage, RequestLoopEnd, NoDriverForRequest, BadRequestType
from ..executors import BaseExecutor
from ..logging.base import get_logger
from ..main.parser import set_pea_parser, set_pod_parser
from ..proto import jina_pb2_grpc, jina_pb2


class GatewayPea:

    def __init__(self, args):
        if not args.proxy and os.name != 'nt':
            os.unsetenv('http_proxy')
            os.unsetenv('https_proxy')
        self.logger = get_logger(self.__class__.__name__, **vars(args))
        self.server = grpc.server(
            AsyncioExecutor(),
            options=[('grpc.max_send_message_length', args.max_message_size),
                     ('grpc.max_receive_message_length', args.max_message_size)])
        if args.allow_spawn:
            self.logger.warning('SECURITY ALERT! this gateway allows SpawnRequest from remote Jina')

        self.p_servicer = self._Pea(args, self.logger)
        jina_pb2_grpc.add_JinaRPCServicer_to_server(self.p_servicer, self.server)
        self.bind_address = '{0}:{1}'.format(args.host, args.port_grpc)
        self.server.add_insecure_port(self.bind_address)
        self._stop_event = threading.Event()
        self.is_ready = threading.Event()

    def __enter__(self):
        self.server.start()
        self.logger.success('gateway is listening at: %s' % self.bind_address)
        self._stop_event.clear()
        self.is_ready.set()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def stop(self):
        self.p_servicer.close()
        self.server.stop(None)
        self._stop_event.set()
        self.logger.success(__stop_msg__)

    def join(self):
        try:
            self._stop_event.wait()
        except KeyboardInterrupt:
            pass

    class _Pea(jina_pb2_grpc.JinaRPCServicer):

        def __init__(self, args, logger):
            super().__init__()
            self.args = args
            self.name = args.name or self.__class__.__name__
            self.logger = logger or get_logger(self.name, **vars(args))
            self.executor = BaseExecutor()
            self.executor.attach(pea=self)
            self.peapods = []

        def recv_callback(self, msg):
            try:
                return self.executor(msg.__class__.__name__)
            except WaitPendingMessage:
                self.logger.error('gateway should not receive partial message, it can not do reduce')
            except RequestLoopEnd:
                self.logger.error('event loop end signal should not be raised in the gateway')
            except NoDriverForRequest:
                # remove envelope and send back the request
                return msg.request

        async def Call(self, request_iterator, context):
            with AsyncZmqlet(self.args, logger=self.logger) as zmqlet:
                # this restricts the gateway can not be the joiner to wait
                # as every request corresponds to one message, #send_message = #recv_message
                send_tasks, recv_tasks = zip(
                    *[(asyncio.create_task(
                        zmqlet.send_message(
                            add_envelope(request, 'gateway', zmqlet.args.identity),
                            sleep=(self.args.sleep_ms / 1000) * idx, )),
                       zmqlet.recv_message(callback=self.recv_callback))
                        for idx, request in enumerate(request_iterator)])

                for r in asyncio.as_completed(recv_tasks):
                    yield await r

        async def Spawn(self, request, context):
            _req = getattr(request, request.WhichOneof('body'))
            if self.args.allow_spawn:
                from . import Pea, Pod
                _req_type = type(_req)
                if _req_type == jina_pb2.SpawnRequest.PeaSpawnRequest:
                    _args = set_pea_parser().parse_known_args(_req.args)[0]
                    self.logger.info('starting a BasePea from a remote request')
                    # we do not allow remote spawn request to spawn a "remote-remote" pea/pod
                    p = Pea(_args, allow_remote=False)
                elif _req_type == jina_pb2.SpawnRequest.PodSpawnRequest:
                    _args = set_pod_parser().parse_known_args(_req.args)[0]
                    self.logger.info('starting a BasePod from a remote request')
                    # need to return the new port and host ip number back
                    # we do not allow remote spawn request to spawn a "remote-remote" pea/pod
                    p = Pod(_args, allow_remote=False)
                    from .remote import peas_args2parsed_pod_req
                    request = peas_args2parsed_pod_req(p.peas_args)
                elif _req_type == jina_pb2.SpawnRequest.ParsedPodSpawnRequest:
                    from .remote import parsed_pod_req2peas_args
                    p = Pod(parsed_pod_req2peas_args(_req), allow_remote=False)
                else:
                    raise BadRequestType('don\'t know how to handle %r' % _req_type)

                with p:
                    self.peapods.append(p)
                    for l in p.log_iterator:
                        request.log_record = l.msg
                        yield request
                self.peapods.remove(p)
            else:
                warn_msg = f'the gateway at {self.args.host}:{self.args.port_grpc} ' \
                           f'does not support remote spawn, please restart it with --allow_spawn'
                request.log_record = warn_msg
                request.status = jina_pb2.SpawnRequest.ERROR_NOTALLOWED
                self.logger.warning(warn_msg)
                for j in range(1):
                    yield request

        def close(self):
            for p in self.peapods:
                p.close()