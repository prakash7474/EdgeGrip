"""Cloud communication package for the Phase 1 prototype."""

from .inference import (
    CloudInferenceError,
    HuggingFaceProvider,
    InferenceResult,
    new_task_id,
    send_image_to_cloud,
    validate_prediction,
)

__all__ = [
    "CloudInferenceError",
    "HuggingFaceProvider",
    "InferenceResult",
    "new_task_id",
    "send_image_to_cloud",
    "validate_prediction",
]
