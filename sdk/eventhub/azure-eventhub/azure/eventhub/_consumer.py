# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------
from __future__ import annotations

import time
import uuid
import logging
from collections import deque
from typing import TYPE_CHECKING, Callable, Dict, Optional, Any, Deque, Union, cast

from ._common import EventData
from ._client_base import ConsumerProducerMixin
from ._utils import create_properties, event_position_selector
from ._constants import (
    EPOCH_SYMBOL,
    TIMEOUT_SYMBOL,
    RECEIVER_RUNTIME_METRIC_SYMBOL,
    GEOREPLICATION_SYMBOL,
)

if TYPE_CHECKING:
    from ._transport._base import AmqpTransport
    from ._pyamqp import types
    from ._pyamqp.message import Message
    from ._pyamqp.authentication import JWTTokenAuth
    from ._pyamqp.client import ReceiveClient

    try:
        from uamqp import ReceiveClient as uamqp_ReceiveClient, Message as uamqp_Message
        from uamqp.types import AMQPType as uamqp_AMQPType
        from uamqp.authentication import JWTTokenAuth as uamqp_JWTTokenAuth
    except ImportError:
        uamqp_ReceiveClient = None
        uamqp_Message = None
        uamqp_AMQPType = None
        uamqp_JWTTokenAuth = None
    from ._consumer_client import EventHubConsumerClient


_LOGGER = logging.getLogger(__name__)


class EventHubConsumer(ConsumerProducerMixin):  # pylint:disable=too-many-instance-attributes
    """
    A consumer responsible for reading EventData from a specific Event Hub
    partition and as a member of a specific consumer group.

    A consumer may be exclusive, which asserts ownership over the partition for the consumer
    group to ensure that only one consumer from that group is reading the from the partition.
    These exclusive consumers are sometimes referred to as "Epoch Consumers."

    A consumer may also be non-exclusive, allowing multiple consumers from the same consumer
    group to be actively reading events from the partition.  These non-exclusive consumers are
    sometimes referred to as "Non-Epoch Consumers."

    Please use the method `create_consumer` on `EventHubClient` for creating `EventHubConsumer`.

    :param client: The parent EventHubConsumerClient.
    :type client: ~azure.eventhub.EventHubConsumerClient
    :param source: The source EventHub from which to receive events.
    :type source: ~azure.eventhub._pyamqp.endpoints.Source or ~uamqp.address.Source
    :keyword event_position: The position from which to start receiving.
    :paramtype event_position: int, str, datetime.datetime
    :keyword int prefetch: The number of events to prefetch from the service
        for processing. Default is 300.
    :keyword int owner_level: The priority of the exclusive consumer. An exclusive
        consumer will be created if owner_level is set.
    :keyword bool track_last_enqueued_event_properties: Indicates whether or not the consumer should request information
        on the last enqueued event on its associated partition, and track that information as events are received.
        When information about the partition's last enqueued event is being tracked, each event received from the
        Event Hubs service will carry metadata about the partition. This results in a small amount of additional
        network bandwidth consumption that is generally a favorable trade-off when considered against periodically
        making requests for partition properties using the Event Hub client.
        It is set to `False` by default.
    """

    def __init__(self, client: "EventHubConsumerClient", source: str, **kwargs: Any) -> None:
        event_position = kwargs.get("event_position", None)
        prefetch = kwargs.get("prefetch", 300)
        owner_level = kwargs.get("owner_level", None)
        keep_alive = kwargs.get("keep_alive", 30)
        auto_reconnect = kwargs.get("auto_reconnect", True)
        track_last_enqueued_event_properties = kwargs.get("track_last_enqueued_event_properties", False)
        idle_timeout = kwargs.get("idle_timeout", None)

        self.running = False
        self.closed = False
        self.stop = False  # used by event processor
        self.handler_ready = False

        self._amqp_transport: "AmqpTransport" = kwargs.pop("amqp_transport")
        self._on_event_received: Callable[[EventData], None] = kwargs["on_event_received"]
        self._client = client
        self._source = source
        self._offset = event_position
        self._offset_inclusive = kwargs.get("event_position_inclusive", False)
        self._prefetch = prefetch
        self._owner_level = owner_level
        self._keep_alive = keep_alive
        self._auto_reconnect = auto_reconnect
        self._retry_policy = self._amqp_transport.create_retry_policy(self._client._config)
        self._reconnect_backoff = 1
        link_properties: Dict[bytes, int] = {}
        self._error = None
        self._timeout = 0
        self._idle_timeout: Optional[float] = \
            (idle_timeout * self._amqp_transport.TIMEOUT_FACTOR) if idle_timeout else None
        self._partition = self._source.split("/")[-1]
        self._name = f"EHConsumer-{uuid.uuid4()}-partition{self._partition}"
        if owner_level is not None:
            link_properties[EPOCH_SYMBOL] = int(owner_level)
        link_property_timeout_ms = (
            self._client._config.receive_timeout or self._timeout
        ) * self._amqp_transport.TIMEOUT_FACTOR
        link_properties[TIMEOUT_SYMBOL] = int(link_property_timeout_ms)
        self._link_properties: Union[Dict[uamqp_AMQPType, uamqp_AMQPType], Dict[types.AMQPTypes, types.AMQPTypes]] = (
            self._amqp_transport.create_link_properties(link_properties)
        )
        self._handler: Optional[Union[uamqp_ReceiveClient, ReceiveClient]] = None
        self._track_last_enqueued_event_properties = track_last_enqueued_event_properties
        self._message_buffer: Deque[uamqp_Message] = deque()
        self._last_received_event: Optional[EventData] = None
        self._receive_start_time: Optional[float] = None

        super(EventHubConsumer, self).__init__()

    def _create_handler(self, auth: Union[uamqp_JWTTokenAuth, JWTTokenAuth]) -> None:
        source = self._amqp_transport.create_source(
            self._source,
            self._offset,
            event_position_selector(self._offset, self._offset_inclusive),
        )
        desired_capabilities = (
            [RECEIVER_RUNTIME_METRIC_SYMBOL,
             GEOREPLICATION_SYMBOL]
             if self._track_last_enqueued_event_properties
             else [GEOREPLICATION_SYMBOL]
        )

        self._handler = self._amqp_transport.create_receive_client(
            config=self._client._config,  # pylint:disable=protected-access
            source=source,
            auth=auth,
            network_trace=self._client._config.network_tracing,  # pylint:disable=protected-access
            link_credit=self._prefetch,
            link_properties=self._link_properties,  # type: ignore
            timeout=self._timeout,
            idle_timeout=self._idle_timeout,
            retry_policy=self._retry_policy,
            keep_alive_interval=self._keep_alive,
            client_name=self._name,
            properties=create_properties(
                self._client._config.user_agent,  # pylint:disable=protected-access
                amqp_transport=self._amqp_transport,
            ),
            desired_capabilities=desired_capabilities,
            streaming_receive=True,
            message_received_callback=self._message_received,
        )

    def _open_with_retry(self) -> None:
        self._do_retryable_operation(self._open, operation_need_param=False)

    def _message_received(self, message: Union[uamqp_Message, Message]) -> None:
        self._message_buffer.append(message)

    def _next_message_in_buffer(self):
        # pylint:disable=protected-access
        message = self._message_buffer.popleft()
        event_data = EventData._from_message(message)
        if self._client._config.network_tracing and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Received message: seq-num: %r, offset: %r, partition-key: %r",
                event_data.sequence_number,
                event_data.offset,
                event_data.partition_key,
            )
        if self._amqp_transport.KIND == "uamqp":
            event_data._uamqp_message = message
        self._last_received_event = event_data
        return event_data

    def _open(self) -> bool:
        """Open the EventHubConsumer/EventHubProducer using the supplied connection.

        :return: Whether the ReceiveClient is open
        :rtype: bool
        """
        # pylint: disable=protected-access
        if not self.running:
            if self._handler:
                self._handler.close()
            auth = self._client._create_auth(auth_uri=self._client._auth_uri)
            self._create_handler(auth)
            conn = self._client._conn_manager.get_connection(  # pylint: disable=protected-access
                endpoint=self._client._address.hostname, auth=auth
            )
            self._handler = cast("ReceiveClient", self._handler)
            self._handler.open(connection=conn)
            self.handler_ready = False
            self.running = True

        if not self.handler_ready:
            if self._handler.client_ready():  # type: ignore
                self.handler_ready = True

        return self.handler_ready

    def receive(self, batch=False, max_batch_size=300, max_wait_time=None):
        retried_times = 0
        max_retries = self._client._config.max_retries  # pylint:disable=protected-access
        self._receive_start_time = self._receive_start_time or time.time()
        deadline = self._receive_start_time + (max_wait_time or 0)
        if len(self._message_buffer) < max_batch_size:
            # TODO: the retry here is a bit tricky as we are using low-level api from the amqp client.
            #  Currently we create a new client with the latest received event's offset per retry.
            #  Ideally we should reuse the same client reestablishing the connection/link with the latest offset.
            while retried_times <= max_retries:
                try:
                    if self._open():
                        self._handler.do_work(batch=self._prefetch)  # type: ignore
                    break
                except Exception as exception:  # pylint: disable=broad-except
                    # If optional dependency is not installed, do not retry.
                    if isinstance(exception, ImportError):
                        raise exception
                    self._amqp_transport.check_link_stolen(self, exception)
                    # TODO: below block hangs when retry_total > 0
                    # need to remove/refactor, issue #27137
                    if not self.running:  # exit by close
                        return
                    if self._last_received_event:
                        self._offset = self._last_received_event.offset
                    last_exception = self._handle_exception(exception, is_consumer=True)
                    retried_times += 1
                    if retried_times > max_retries:
                        _LOGGER.info(
                            "%r operation has exhausted retry. Last exception: %r.",
                            self._name,
                            last_exception,
                        )
                        raise last_exception from None
        if (
            len(self._message_buffer) >= max_batch_size
            or (self._message_buffer and not max_wait_time)
            or (deadline <= time.time() and max_wait_time)
        ):
            if batch:
                events_for_callback = []
                for _ in range(min(max_batch_size, len(self._message_buffer))):
                    events_for_callback.append(self._next_message_in_buffer())
                self._on_event_received(events_for_callback)
            else:
                self._on_event_received(self._next_message_in_buffer() if self._message_buffer else None)
            self._receive_start_time = None
