"""Config-driven mapping from an inference decision to an actuator action.

A decision is ``(label, score)``. Rules are checked in order and the first
match wins; when nothing matches, the configured default (``hold``) applies.
``hold`` sends nothing to the device, so an unknown label can never move the
hand.

Configuration is JSON::

    {
      "version": 1,
      "default": {"action": "hold"},
      "rules": [
        {"label": "part", "min_score": 0.6, "action": "grip",
         "parameters": {"speed": 25}}
      ]
    }

Rules with a ``min_score``/``max_score`` constraint never match when the score
is unavailable (``None``): an unknown score is not evidence.
"""

from __future__ import annotations

import copy
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from actuator.protocol import ProtocolError, validate_parameters

POLICY_ACTIONS = ("grip", "release", "move_to", "stop", "hold")
POLICY_ENV_VAR = "ACTUATION_POLICY_PATH"

DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "default": {"action": "hold"},
    "rules": [
        {
            "label": "part",
            "min_score": 0.60,
            "action": "grip",
            "parameters": {"speed": 25},
        },
        {
            "label": "component",
            "min_score": 0.60,
            "action": "grip",
            "parameters": {"speed": 25},
        },
        {
            "label": "bin",
            "min_score": 0.60,
            "action": "release",
            "parameters": {"speed": 25},
        },
    ],
}

_RULE_KEYS = {"label", "action", "parameters", "min_score", "max_score"}
_TOP_LEVEL_KEYS = {"version", "default", "rules"}


@dataclass(frozen=True)
class PolicyAction:
    """The action chosen for one decision."""

    action: str
    parameters: dict[str, int] = field(default_factory=dict)
    label: str | None = None
    score: float | None = None
    matched: str = "default"

    def __post_init__(self) -> None:
        if self.action not in POLICY_ACTIONS:
            raise ValueError(f"unknown policy action: {self.action}")


@dataclass(frozen=True)
class _Rule:
    label: str
    action: str
    parameters: dict[str, int]
    min_score: float | None
    max_score: float | None


def _checked_action(entry: Any, where: str) -> tuple[str, dict[str, int]]:
    if not isinstance(entry, Mapping):
        raise ValueError(f"{where}: action entry must be an object")
    action = entry.get("action")
    if action not in POLICY_ACTIONS:
        raise ValueError(f"{where}: action must be one of {', '.join(POLICY_ACTIONS)}")
    parameters = entry.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise ValueError(f"{where}: parameters must be an object")
    parameters = dict(parameters)
    if action == "hold":
        if parameters:
            raise ValueError(f"{where}: hold takes no parameters")
        return action, {}
    try:
        validate_parameters(action, parameters)
    except ProtocolError as exc:
        raise ValueError(f"{where}: {exc}") from exc
    if action == "move_to" and "angle" not in parameters:
        raise ValueError(f"{where}: move_to requires an angle")
    return action, parameters


def _score(value: Any, where: str, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: {field_name} must be a number")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{where}: {field_name} must be between 0 and 1")
    return number


class Policy:
    """Validated rule set. Build one per process; :meth:`decide` is read-only."""

    def __init__(self, config: Mapping[str, Any] | None = None):
        if config is None:
            config = DEFAULT_POLICY
        if not isinstance(config, Mapping):
            raise ValueError("policy config must be an object")
        unknown = set(config) - _TOP_LEVEL_KEYS
        if unknown:
            raise ValueError(f"policy config has unknown keys: {', '.join(sorted(unknown))}")

        default_entry = config.get("default") or {"action": "hold"}
        self.default_action, self.default_parameters = _checked_action(default_entry, "default")

        raw_rules = config.get("rules") or []
        if not isinstance(raw_rules, list):
            raise ValueError("policy rules must be a list")
        rules: list[_Rule] = []
        for index, raw in enumerate(raw_rules):
            where = f"rule {index}"
            if not isinstance(raw, Mapping):
                raise ValueError(f"{where}: must be an object")
            unknown_rule = set(raw) - _RULE_KEYS
            if unknown_rule:
                raise ValueError(
                    f"{where}: unknown keys {', '.join(sorted(unknown_rule))}"
                )
            label = raw.get("label")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"{where}: label must be a non-empty string")
            action, parameters = _checked_action(raw, where)
            min_score = _score(raw.get("min_score"), where, "min_score")
            max_score = _score(raw.get("max_score"), where, "max_score")
            if min_score is not None and max_score is not None and min_score > max_score:
                raise ValueError(f"{where}: min_score is greater than max_score")
            rules.append(
                _Rule(
                    label=label.strip().lower(),
                    action=action,
                    parameters=parameters,
                    min_score=min_score,
                    max_score=max_score,
                )
            )
        self.rules = tuple(rules)

    @classmethod
    def from_file(cls, path: str | Path) -> "Policy":
        """Load and validate a policy JSON file."""
        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"policy file not found: {source}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"policy file {source} is not valid JSON: {exc}") from exc
        return cls(raw)

    def decide(self, label: str | None, score: float | None = None) -> PolicyAction:
        """Return the action for one decision. Never raises on unknown labels."""
        normalized = str(label).strip().lower() if label is not None else ""
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError) as exc:
                raise ValueError("score must be numeric or None") from exc
            if not math.isfinite(score):
                # NaN would pass every "score >= min" test; treat it as invalid.
                raise ValueError("score must be finite")
        for rule in self.rules:
            if rule.label != normalized:
                continue
            if rule.min_score is not None:
                if score is None or score < rule.min_score:
                    continue
            if rule.max_score is not None:
                if score is None or score > rule.max_score:
                    continue
            return PolicyAction(
                action=rule.action,
                parameters=dict(rule.parameters),
                label=normalized or None,
                score=score,
                matched=rule.label,
            )
        return PolicyAction(
            action=self.default_action,
            parameters=dict(self.default_parameters),
            label=normalized or None,
            score=score,
            matched="default",
        )

    def describe(self) -> dict[str, Any]:
        """Return the effective config (safe to log; no secrets involved)."""
        return {
            "default": {"action": self.default_action,
                        "parameters": dict(self.default_parameters)},
            "rules": [
                {
                    "label": rule.label,
                    "action": rule.action,
                    "parameters": dict(rule.parameters),
                    "min_score": rule.min_score,
                    "max_score": rule.max_score,
                }
                for rule in self.rules
            ],
        }


def load_policy(path: str | Path | None = None) -> Policy:
    """Load ``path``, else ``ACTUATION_POLICY_PATH``, else the built-in default."""
    if path is None:
        env_value = os.getenv(POLICY_ENV_VAR, "").strip()
        path = env_value or None
    if path:
        return Policy.from_file(path)
    return Policy(copy.deepcopy(DEFAULT_POLICY))
