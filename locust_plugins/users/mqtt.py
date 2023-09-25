from __future__ import annotations

import random
import time
import typing

from locust import User
from locust.env import Environment
from locust_plugins import missing_extra

try:
    import paho.mqtt.client as mqtt
except ModuleNotFoundError:
    missing_extra("paho", "mqtt")

if typing.TYPE_CHECKING:
    from paho.mqtt.client import MQTTMessageInfo
    from paho.mqtt.properties import Properties
    from paho.mqtt.subscribeoptions import SubscribeOptions


# A SUBACK response for MQTT can only contain 0x00, 0x01, 0x02, or 0x80. 0x80
# indicates a failure to subscribe.
#
# http://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html#_Figure_3.26_-
SUBACK_FAILURE = 0x80
REQUEST_TYPE = "MQTT"


def _generate_random_id(length: int, alphabet: str = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    """Generate a random ID from the given alphabet.

    Args:
        length: the number of random characters to generate.
        alphabet: the pool of random characters to choose from.
    """
    return "".join(random.choice(alphabet) for _ in range(length))


class PublishedMessageContext(typing.NamedTuple):
    """Stores metadata about outgoing published messages."""

    qos: int
    topic: str
    start_time: float
    payload_size: int


class SubscribeContext(typing.NamedTuple):
    """Stores metadata about outgoing published messages."""

    qos: int
    topic: str
    start_time: float
    message_in_events: bool


class SubscriptionContext(typing.NamedTuple):
    """Stores metadata about current subscriptions."""

    granted_qos: list[int]


class MqttClient(mqtt.Client):
    def __init__(
        self,
        *args,
        environment: Environment,
        client_id: typing.Optional[str] = None,
        qos_in_eventname: bool = True,
        topic_in_eventname: bool = True,
        **kwargs,
    ):
        """Initializes a paho.mqtt.Client for use in Locust swarms.

        This class passes most args & kwargs through to the underlying
        paho.mqtt constructor.

        Args:
            environment: the Locust environment with which to associate events.
            client_id: the MQTT Client ID to use in connecting to the broker.
                If not set, one will be randomly generated.
        """
        # If a client ID is not provided, this class will randomly generate an ID
        # of the form: `locust-[0-9a-zA-Z]{16}` (i.e., `locust-` followed by 16
        # random characters, so that the resulting client ID does not exceed the
        # specification limit of 23 characters).

        # This is done in this wrapper class so that this locust client can
        # self-identify when firing requests, since some versions of MQTT will
        # have the broker assign IDs to clients that do not provide one: in this
        # case, there is no way to retrieve the client ID.

        # See https://github.com/eclipse/paho.mqtt.python/issues/237
        if not client_id:
            self.client_id = f"locust-{_generate_random_id(16)}"
        else:
            self.client_id = client_id

        super().__init__(*args, client_id=self.client_id, **kwargs)
        self.environment = environment
        self.qos_in_eventname = qos_in_eventname
        self.topic_in_eventname = topic_in_eventname
        self.on_publish = self._on_publish_cb
        self.on_subscribe = self._on_subscribe_cb
        self.on_disconnect = self._on_disconnect_cb
        self.on_connect = self._on_connect_cb
        self.on_message = self._on_message_cb
        self._publish_requests: dict[int, PublishedMessageContext] = {}
        self._subscribe_requests: dict[int, SubscribeContext] = {}
        self._subscriptions: dict[str, SubscriptionContext] = {}

    def _on_publish_cb(
        self,
        client: mqtt.Client,
        userdata: typing.Any,
        mid: int,
    ):
        cb_time = time.time()
        try:
            request_context = self._publish_requests.pop(mid)
        except KeyError:
            # we shouldn't hit this block of code
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name="publish",
                response_time=0,
                response_length=0,
                exception=AssertionError(f"Could not find message data for mid '{mid}' in _on_publish_cb."),
                context={
                    "client_id": self.client_id,
                    "mid": mid,
                },
            )
        else:
            # fire successful publish event
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name=self._generate_mqtt_event_name("publish", request_context.qos, request_context.topic),
                response_time=(cb_time - request_context.start_time) * 1000,
                response_length=request_context.payload_size,
                exception=None,
                context={
                    "client_id": self.client_id,
                    **request_context._asdict(),
                },
            )

    def _on_subscribe_cb(
        self,
        client: mqtt.Client,
        userdata: typing.Any,
        mid: int,
        granted_qos: list[int],
    ):
        cb_time = time.time()
        try:
            request_context = self._subscribe_requests.pop(mid)
        except KeyError:
            # we shouldn't hit this block of code
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name="subscribe",
                response_time=0,
                response_length=0,
                exception=AssertionError(f"Could not find message data for mid '{mid}' in _on_subscribe_cb."),
                context={
                    "client_id": self.client_id,
                    "mid": mid,
                },
            )
        else:
            if SUBACK_FAILURE in granted_qos:
                self.environment.events.request.fire(
                    request_type=REQUEST_TYPE,
                    name=self._generate_mqtt_event_name("subscribe", request_context.qos, request_context.topic),
                    response_time=(cb_time - request_context.start_time) * 1000,
                    response_length=0,
                    exception=AssertionError(f"Broker returned an error response during subscription: {granted_qos}"),
                    context={
                        "client_id": self.client_id,
                        **request_context._asdict(),
                    },
                )
            else:
                # fire successful subscribe event
                self.environment.events.request.fire(
                    request_type=REQUEST_TYPE,
                    name=self._generate_mqtt_event_name("subscribe", request_context.qos, request_context.topic),
                    response_time=(cb_time - request_context.start_time) * 1000,
                    response_length=0,
                    exception=None,
                    context={
                        "client_id": self.client_id,
                        **request_context._asdict(),
                    },
                )
                if request_context.message_in_events:
                    self._subscriptions[request_context.topic] = SubscriptionContext(granted_qos=granted_qos)

    def _on_disconnect_cb(
        self,
        client: mqtt.Client,
        userdata: typing.Any,
        rc: int,
    ):
        if rc != 0:
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name="disconnect",
                response_time=0,
                response_length=0,
                exception=rc,
                context={
                    "client_id": self.client_id,
                },
            )
        else:
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name="disconnect",
                response_time=0,
                response_length=0,
                exception=None,
                context={
                    "client_id": self.client_id,
                },
            )

    def _on_connect_cb(
        self,
        client: mqtt.Client,
        userdata: typing.Any,
        flags: dict[str, int],
        rc: int,
    ):
        if rc != 0:
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name="connect",
                response_time=0,
                response_length=0,
                exception=rc,
                context={
                    "client_id": self.client_id,
                },
            )
        else:
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name="connect",
                response_time=0,
                response_length=0,
                exception=None,
                context={
                    "client_id": self.client_id,
                },
            )

    def _on_message_cb(self, client: mqtt.Client, userdata: typing.Any, message: mqtt.MQTTMessage):
        try:
            subscription_context = self._subscriptions[message.topic]
            cb_time = time.time()
            message_size = len(message.payload)
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name=self._generate_mqtt_event_name("message_in", message.qos, message.topic),
                response_time=0,
                response_length=message_size,
                exception=None,
                context={
                    "client_id": self.client_id,
                    "topic": message.topic,
                    "qos": message.qos,
                    "receive_time": cb_time,
                },
            )
        except KeyError:
            pass

    def publish(
        self,
        topic: str,
        payload: typing.Optional[bytes] = None,
        qos: int = 0,
        retain: bool = False,
        properties: typing.Optional[Properties] = None,
    ) -> MQTTMessageInfo:
        """Publish a message to the MQTT broker.

        This method wraps the underlying paho-mqtt client's method in order to
        set up & fire Locust events.
        """
        request_context = PublishedMessageContext(
            qos=qos,
            topic=topic,
            start_time=time.time(),
            payload_size=len(payload) if payload else 0,
        )

        publish_info = super().publish(topic, payload=payload, qos=qos, retain=retain)

        if publish_info.rc != mqtt.MQTT_ERR_SUCCESS:
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name=self._generate_mqtt_event_name("publish", request_context.qos, request_context.topic),
                response_time=0,
                response_length=0,
                exception=publish_info.rc,
                context={
                    "client_id": self.client_id,
                    **request_context._asdict(),
                },
            )
        else:
            # store this for use in the on_publish callback
            self._publish_requests[publish_info.mid] = request_context

        return publish_info

    def subscribe(
        self,
        topic: str,
        qos: int = 0,
        options: typing.Optional[SubscribeOptions] = None,
        properties: typing.Optional[Properties] = None,
        message_in_events: bool = False,
    ) -> typing.Tuple[int, typing.Optional[int]]:
        """Subscribe to a given topic.

        This method wraps the underlying paho-mqtt client's method in order to
        set up & fire Locust events.
        """
        request_context = SubscribeContext(
            qos=qos,
            topic=topic,
            start_time=time.time(),
            message_in_events=message_in_events,
        )

        result, mid = super().subscribe(topic=topic, qos=qos)

        if result != mqtt.MQTT_ERR_SUCCESS:
            self.environment.events.request.fire(
                request_type=REQUEST_TYPE,
                name=self._generate_mqtt_event_name("subscribe", request_context.qos, request_context.topic),
                response_time=0,
                response_length=0,
                exception=result,
                context={
                    "client_id": self.client_id,
                    **request_context._asdict(),
                },
            )
        else:
            self._subscribe_requests[mid] = request_context

        return result, mid


class MqttUser(User):
    abstract = True

    host = "localhost"
    port = 1883
    transport = "tcp"
    ws_path = "/mqtt"
    tls_context = None
    client_cls: typing.Type[MqttClient] = MqttClient
    client_id = None
    username = None
    password = None
    qos_in_eventname = True
    topic_in_eventname = True

    def __init__(self, environment: Environment):
        super().__init__(environment)
        self.client: MqttClient = self.client_cls(
            environment=self.environment,
            transport=self.transport,
            client_id=self.client_id,
            qos_in_eventname=self.qos_in_eventname,
            topic_in_eventname=self.topic_in_eventname,
        )

        if self.tls_context:
            self.client.tls_set_context(self.tls_context)

        if self.transport == "websockets" and self.ws_path:
            self.client.ws_set_options(path=self.ws_path)

        if self.username and self.password:
            self.client.username_pw_set(
                username=self.username,
                password=self.password,
            )

        self.client.connect_async(
            host=self.host,
            port=self.port,
        )
        self.client.loop_start()
