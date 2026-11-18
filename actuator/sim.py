"""Optional PyBullet actuator.

The simulation is kinematic: joint positions are set directly, so it shows the
commanded pose but does **not** model grasp contact forces. It exists to watch
the policy's motion in a GUI (``SimActuator(gui=True)``) without hardware.

PyBullet is optional and imported lazily.
"""

from __future__ import annotations

import math
import os
import time

from actuator.base import ActuationResult, Actuator, new_command_id, utc_now_iso
from actuator.protocol import DEFAULT_SPEED


class SimActuator(Actuator):
    """PyBullet-backed kinematic hand: rotating wrist plus two fingers."""

    name = "sim"

    def __init__(
        self,
        *,
        gui: bool = False,
        grip_gap_m: float = 0.012,
        release_gap_m: float = 0.040,
    ):
        try:
            import pybullet as p  # optional dependency
            import pybullet_data
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "SimActuator requires the optional pybullet package (pip install pybullet)"
            ) from exc
        self._p = p
        self.grip_gap_m = float(grip_gap_m)
        self.release_gap_m = float(release_gap_m)
        self.angle = 0
        self.gripping = False
        self.speed = 0
        self.motion = "idle"

        self._client = p.connect(p.GUI if gui else p.DIRECT)
        try:
            p.setGravity(0, 0, -9.81, physicsClientId=self._client)
            plane = os.path.join(pybullet_data.getDataPath(), "plane.urdf")
            self._plane = p.loadURDF(plane, useMaximalCoordinates=True,
                                     physicsClientId=self._client)
            palm_visual = p.createVisualShape(
                p.GEOM_BOX, halfExtents=[0.03, 0.03, 0.02],
                rgbaColor=[0.35, 0.35, 0.42, 1.0], physicsClientId=self._client,
            )
            wrist_visual = p.createVisualShape(
                p.GEOM_CYLINDER, radius=0.025, length=0.04,
                rgbaColor=[0.5, 0.5, 0.55, 1.0], physicsClientId=self._client,
            )
            finger_visual = p.createVisualShape(
                p.GEOM_BOX, halfExtents=[0.02, 0.006, 0.02],
                rgbaColor=[0.85, 0.55, 0.2, 1.0], physicsClientId=self._client,
            )
            # Link 0 = wrist (revolute about z), links 1/2 = fingers (prismatic,
            # axes point toward the palm centre so a positive displacement closes).
            self._body = p.createMultiBody(
                baseMass=0,
                baseCollisionShapeIndex=-1,
                baseVisualShapeIndex=palm_visual,
                basePosition=[0, 0, 0.15],
                useFixedBase=True,
                linkMasses=[0.05, 0.02, 0.02],
                linkCollisionShapeIndices=[-1, -1, -1],
                linkVisualShapeIndices=[wrist_visual, finger_visual, finger_visual],
                linkPositions=[[0, 0, 0.03], [0, 0.045, 0.02], [0, -0.045, 0.02]],
                linkOrientations=[[0, 0, 0, 1]] * 3,
                linkInertialFramePositions=[[0, 0, 0]] * 3,
                linkInertialFrameOrientations=[[0, 0, 0, 1]] * 3,
                linkParentIndices=[0, 1, 1],
                linkJointTypes=[p.JOINT_REVOLUTE, p.JOINT_PRISMATIC, p.JOINT_PRISMATIC],
                linkJointAxis=[[0, 0, 1], [0, -1, 0], [0, 1, 0]],
                physicsClientId=self._client,
            )
            self._set_gap(self.release_gap_m)
        except Exception:
            try:
                p.disconnect(self._client)
            except Exception:
                pass
            raise

    # ------------------------------------------------------------------ commands
    def grip(self, *, speed: int | None = None,
             command_id: str | None = None) -> ActuationResult:
        started = time.perf_counter()
        self.gripping = True
        self._set_gap(self.grip_gap_m)
        self.speed = DEFAULT_SPEED if speed is None else int(speed)
        self.motion = "grip"
        return self._result(command_id, "grip", {} if speed is None else {"speed": speed}, started)

    def release(self, *, speed: int | None = None,
                command_id: str | None = None) -> ActuationResult:
        started = time.perf_counter()
        self.gripping = False
        self._set_gap(self.release_gap_m)
        self.speed = DEFAULT_SPEED if speed is None else int(speed)
        self.motion = "release"
        return self._result(command_id, "release",
                            {} if speed is None else {"speed": speed}, started)

    def move_to(self, angle: int, *, speed: int | None = None,
                command_id: str | None = None) -> ActuationResult:
        started = time.perf_counter()
        self.angle = int(angle)
        self._p.resetJointState(self._body, 0, math.radians(self.angle),
                                physicsClientId=self._client)
        self.speed = DEFAULT_SPEED if speed is None else int(speed)
        self.motion = "move_to"
        parameters = {"angle": self.angle}
        if speed is not None:
            parameters["speed"] = int(speed)
        return self._result(command_id, "move_to", parameters, started)

    def stop(self, *, command_id: str | None = None) -> ActuationResult:
        started = time.perf_counter()
        self.speed = 0
        self.motion = "stop"
        return self._result(command_id, "stop", {}, started)

    def heartbeat(self, *, command_id: str | None = None) -> ActuationResult:
        started = time.perf_counter()
        for _ in range(4):
            self._p.stepSimulation(physicsClientId=self._client)
        return self._result(command_id, "heartbeat", {}, started)

    # ------------------------------------------------------------------ state
    def status(self) -> dict:
        return {
            "angle": self.angle,
            "gripping": self.gripping,
            "speed": self.speed,
            "motion": self.motion,
            "gripper_gap_m": self.grip_gap_m if self.gripping else self.release_gap_m,
        }

    def close(self) -> None:
        try:
            self._p.disconnect(self._client)
        except Exception:
            pass

    # ----------------------------------------------------------------- internal
    def _set_gap(self, gap_m: float) -> None:
        half = float(gap_m) / 2.0
        self._p.resetJointState(self._body, 1, half, physicsClientId=self._client)
        self._p.resetJointState(self._body, 2, half, physicsClientId=self._client)
        for _ in range(4):
            self._p.stepSimulation(physicsClientId=self._client)

    def _result(self, command_id: str | None, action: str,
                parameters: dict[str, int], started: float) -> ActuationResult:
        finished = time.perf_counter()
        return ActuationResult(
            command_id=command_id or new_command_id(),
            action=action,
            parameters=dict(parameters),
            acked=True,
            reason="ack",
            detail="sim",
            sent_at=utc_now_iso(),
            acked_at=utc_now_iso(),
            latency_ms=round((finished - started) * 1000.0, 3),
            sent_perf=started,
            acked_perf=finished,
        )
