import eventlet
from eventlet.greenpool import GreenPool
from eventlet.event import Event
from eventlet.semaphore import Semaphore
import greenlet
from kombu.mixins import ConsumerMixin

import newrpc
from newrpc import entities
from newrpc.common import UIDGEN
from newrpc.messaging import get_consumers, process_message
from newrpc.dependencies import inject_dependencies


class Service(ConsumerMixin):
    def __init__(self, controllercls,
            connection, exchange, topic,
            pool=None, poolsize=1000):
        self.nodeid = UIDGEN()

        if pool is None:
            self.procpool = GreenPool(size=poolsize)
        else:
            self.procpool = pool

        self.connection = connection
        self.controller = controllercls()
        self.service = self.controller
        self.topic = topic
        self.greenlet = None
        self.messagesem = Semaphore()
        self.consume_ready = Event()

        node_topic = "{}.{}".format(self.topic, self.nodeid)
        self.nova_queues = [entities.get_topic_queue(exchange, topic),
                       entities.get_topic_queue(exchange, node_topic),
                       entities.get_fanout_queue(topic), ]

        self._channel = None
        self._consumers = None

        inject_dependencies(self.controller, connection)

    def start(self):
        # self.connection = newrpc.create_connection()
        if self.greenlet is not None and not self.greenlet.dead:
            raise RuntimeError()
        self.greenlet = eventlet.spawn(self.run)

    def get_consumers(self, Consumer, channel):
        nova_consumer = Consumer(self.nova_queues, callbacks=[self.on_nova_message, ])

        consume_consumers = get_consumers(Consumer, self.controller,
                                                self.on_consume_message)

        return [nova_consumer] + list(consume_consumers)

    def on_consume_ready(self, connection, channel, consumers, **kwargs):
        self._consumers = consumers
        self._channel = channel
        self.consume_ready.send(None)

    def on_consume_end(self, connection, channel):
        self.consume_ready.reset()

    def on_nova_message(self, body, message):
        # need a semaphore to stop killing between message ack()
        # and spawning process.
        # TODO: kill() does not use the semaphore
        with self.messagesem:
            # TODO: if the procpool has been exhausted this will block.
            # Why do we accept messages when we cannot handle them?
            # Maybe we should spawn at a different time and only spawn if we have
            # non-idle workers left. Thus, messages will be picked up only by
            # workers which indeed can handle the message.
            self.procpool.spawn(self.handle_request, body)
            message.ack()

    def on_consume_message(self, consumer_config, consumer_method, body, message):
        # need a semaphore to stop killing between message ack()
        # and spawning process.
        # TODO: kill() does not use the semaphore
        with self.messagesem:
            # TODO: Why does message ack work from the spawned thread?
            # TODO: if the procpool has been exhausted this will block.
            # Why do we accept messages when we cannot handle them?
            # Maybe we should spawn at a different time and only spawn if we have
            # non-idle workers left. Thus, messages will be picked up only by
            # workers which indeed can handle the message.
            self.procpool.spawn(process_message, consumer_config,
                                consumer_method, body, message)

    def handle_request(self, body):
        newrpc.process_message(self.connection, self.controller, body)

    def wait(self):
        try:
            self.greenlet.wait()
        except greenlet.GreenletExit:
            pass
        return self.procpool.waitall()

    def kill(self):
        if self.greenlet is not None and not self.greenlet.dead:
            self.should_stop = True
            #with self.messagesem:
                #self.greenlet.kill()
            self.greenlet.wait()
        if self._consumers:
            for c in self._consumers:
                c.cancel()
        if self._channel is not None:
            self._channel.close()

    def link(self, *args, **kwargs):
        return self.greenlet.link(*args, **kwargs)

    def kill_processes(self):
        for g in self.procpool.coroutines_running:
            g.kill()
