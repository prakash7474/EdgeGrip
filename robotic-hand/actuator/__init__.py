"""Phase 5b actuator layer: interface, stub/sim/serial devices, policy, grip.

No module here talks to hardware at import time. ``SimActuator`` needs the
optional ``pybullet`` package, ``SerialActuator`` needs the optional ``pyserial``
package, and both are imported lazily so the offline test suite works without
them.
"""

from actuator.base import (
    ActuationResult,
    Actuator,
    command_id_for_task,
    new_command_id,
)
from actuator.fake_device import FakeDevice, FakeTransport, ManualClock
from actuator.grip import ActuationGrip, GripOutcome
from actuator.log import ActuationLog
from actuator.policy import DEFAULT_POLICY, Policy, PolicyAction, load_policy
from actuator.protocol import (
    ACK,
    NACK,
    Frame,
    ProtocolError,
    Reply,
    format_frame,
    parse_frame,
    parse_reply,
)
from actuator.serial_actuator import SerialActuator
from actuator.sim import SimActuator
from actuator.stub import StubActuator

__all__ = [
    "ACK",
    "NACK",
    "ActuationGrip",
    "ActuationLog",
    "ActuationResult",
    "Actuator",
    "DEFAULT_POLICY",
    "FakeDevice",
    "FakeTransport",
    "Frame",
    "GripOutcome",
    "ManualClock",
    "Policy",
    "PolicyAction",
    "ProtocolError",
    "Reply",
    "SerialActuator",
    "SimActuator",
    "StubActuator",
    "command_id_for_task",
    "format_frame",
    "load_policy",
    "new_command_id",
    "parse_frame",
    "parse_reply",
]
