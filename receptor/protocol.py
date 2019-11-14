import asyncio
import datetime
import functools
import json
import logging
import time
import uuid

from .exceptions import ReceptorBufferError
from .messages import envelope
from .stats import connected_peers_guage

logger = logging.getLogger(__name__)

DELIM = b"\x1b[K"
SIZEB = b"\x1b[%dD"


class DataBuffer:
    def __init__(self, loop=None, deserializer=json.loads):
        self.q = asyncio.Queue(loop=loop)
        self.data_buffer = b""
        self.deserializer = deserializer

    def add(self, data):
        self.data_buffer = self.data_buffer + data
        *ready, self.data_buffer = self.data_buffer.rsplit(DELIM)
        for chunk in ready:
            self.q.put_nowait(chunk)

    async def get(self):
        return self.deserializer(await self.q.get())


class BaseProtocol(asyncio.Protocol):
    def __init__(self, receptor, loop):
        self.receptor = receptor
        self.loop = loop
        self.id = None
        self.meta = None

    def __str__(self):
        return f"<Connection {self.id} {self.transport}"

    async def watch_queue(self):
        '''
        Watches the buffer for this connection for messages delivered from other
        parts of Receptor (forwarded messages for example) for messages to send
        over the connection.
        '''
        buffer_mgr = self.receptor.config.components_buffer_manager
        buffer_obj = buffer_mgr.get_buffer_for_node(self.id, self.receptor)
        while not self.transport.is_closing():
            try:
                msg = buffer_obj.pop()
                self.transport.write(msg + DELIM)
            except IndexError:
                await asyncio.sleep(0.1)
            except ReceptorBufferError as e:
                logger.exception("Receptor Buffer Read Error: {}".format(e))
                # TODO: We need to try to send this message along somewhere else
                # and record the failure somewhere
                self.transport.close()
                return
            except Exception as e:
                logger.exception("Error received trying to write to {}: {}".format(self.id, e))
                buffer_obj.push(msg)
                self.transport.close()
                return

    def connection_made(self, transport):
        self.peername = transport.get_extra_info('peername')
        self.transport = transport
        connected_peers_guage.inc()
        self.incoming_buffer = DataBuffer(loop=self.loop)
        self.loop.create_task(self.wait_greeting())

    def connection_lost(self, exc):
        connected_peers_guage.dec()
        self.receptor.remove_connection(self)

    def data_received(self, data):
        logger.debug(data)
        self.incoming_buffer.add(data)

    async def wait_greeting(self):
        '''
        Initialized when the connection is established to handle the greeting
        before transitioning to message processing.
        '''
        logger.debug('Looking for handshake...')
        data = await self.incoming_buffer.get()
        logger.debug(data)
        if data["cmd"] == "HI":
            self.handle_handshake(data)
            logger.debug("handshake complete, starting normal handle loop")
        else:
            logger.error("Handshake failed!")
            self.transport.close()

    def handle_handshake(self, data):
        self.id = data["id"]
        self.meta = data.get("meta", {})
        self.receptor.add_connection(self)
        self.loop.create_task(self.watch_queue())
        self.loop.create_task(self.receptor.message_handler(self.incoming_buffer))

    def send_handshake(self):
        msg = json.dumps({
            "cmd": "HI",
            "id": self.receptor.node_id,
            "expire_time": time.time() + 10,
            "meta": dict(capabilities=self.receptor.work_manager.get_capabilities(),
                         groups=self.receptor.config.node_groups,
                         work=self.receptor.work_manager.get_work())
        }).encode("utf-8")
        self.transport.write(msg + DELIM)


class BasicProtocol(BaseProtocol):
    def connection_made(self, transport):
        super().connection_made(transport)
        logger.info('Connection from {}'.format(self.peername))

    def handle_handshake(self, data):
        super().handle_handshake(data)
        logger.debug("Received handshake from client with id %s, responding...", data["id"])
        self.send_handshake()
        self.receptor.send_route_advertisement()


async def create_peer(receptor, loop, host, port):
    while True:
        try:
            await loop.create_connection(
                lambda: BasicClientProtocol(receptor, loop), host, port, ssl=receptor.config.get_client_ssl_context())
            break
        except Exception:
            logger.exception("Connection Refused: {}:{}".format(host, port))
            await asyncio.sleep(5)


class BasicClientProtocol(BaseProtocol):
    def connection_made(self, transport):
        super().connection_made(transport)
        logger.info("Connection to %s", self.peername)
        logger.debug("Sending handshake to server...")
        self.send_handshake()

    def connection_lost(self, exc):
        logger.info('Connection lost with the server...')
        super().connection_lost(exc)
        info = self.transport.get_extra_info('peername')
        self.loop.create_task(create_peer(self.receptor, self.loop, info[0], info[1]))

    def handle_handshake(self, data):
        super().handle_handshake(data)
        logger.debug("Received handshake from server with id %s", data["id"])
        self.receptor.send_route_advertisement()


class BasicControllerProtocol(asyncio.Protocol):

    def __init__(self, receptor, loop):
        self.receptor = receptor
        self.loop = loop

    def connection_made(self, transport):
        self.transport = transport
        connected_peers_guage.inc()
        if self not in self.receptor.controller_connections:
            self.receptor.controller_connections.append(self)

    def connection_lost(self, exc):
        connected_peers_guage.dec()
        if self in self.receptor.controller_connections:
            self.receptor.controller_connections.remove(self)

    def emit_response(self, response):
        emit_task = self.loop.create_task(response.sign_and_serialize())
        emit_task.add_done_callback(
            functools.partial(self._do_emit_callback)
        )

    def _do_emit_callback(self, fut):
        res = fut.result()
        self.transport.write(res.encode() + DELIM)

    def data_received(self, data):
        recipient, directive, payload = data.rstrip(DELIM).decode('utf8').split('\n', 2)
        message_id = str(uuid.uuid4())
        logger.info(f'{message_id}: Sending {directive} to {recipient}')
        sent_timestamp = datetime.datetime.utcnow()
        inner_env = envelope.InnerEnvelope(
            receptor=self.receptor,
            message_id=message_id,
            sender=self.receptor.node_id,
            recipient=recipient,
            message_type='directive',
            timestamp=sent_timestamp.isoformat(),
            raw_payload=payload,
            directive=directive,
        )
        # TODO: Persistent registry?
        send_task = self.loop.create_task(
            self.receptor.router.send(
                inner_env,
                expected_response=True
            )
        )
        send_task.add_done_callback(
            functools.partial(self._data_received_callback, inner_env)
        )

    def _data_received_callback(self, inner_env, fut):
        try:
            fut.result()
        except Exception as e:
            err_resp = envelope.InnerEnvelope.make_response(
                receptor=self.receptor,
                recipient=inner_env.sender,
                payload=str(e),
                in_response_to=inner_env.message_id,
                ttl=inner_env.ttl,
                serial=inner_env.serial,
                code=1,
            )
            self.emit_response(err_resp)
