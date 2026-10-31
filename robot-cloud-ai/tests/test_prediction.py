"""TC1.3 — prediction structure validation and labelled-label matching."""

from __future__ import annotations

import pytest

from cloud.inference import CloudInferenceError, send_image_to_cloud, validate_prediction
from reporting import labels_match, summarise


@pytest.mark.parametrize(
    ("predicted", "expected", "should_match"),
    [
        ("tabby, tabby cat", "tabby cat", True),
        ("Siberian husky", "Siberian husky", True),
        ("banana", "banana", True),
        ("tiger cat", "cat", True),
        ("cat", "tiger cat", True),
        ("Granny Smith", "granny smith", True),
        ("golden retriever", "cat", False),
        ("cat", "caterpillar", False),
        ("", "cat", False),
        ("cat", "", False),
    ],
)
def test_labels_match_handles_aliases_and_taxonomy_width(predicted, expected, should_match):
    assert labels_match(predicted, expected) is should_match


def test_validate_prediction_selects_highest_scoring_label():
    label, confidence = validate_prediction(
        [{"label": "cat", "score": 0.2}, {"label": "dog", "score": 0.8}]
    )
    assert label == "dog"
    assert confidence == 0.8


def test_validate_prediction_accepts_missing_scores():
    label, confidence = validate_prediction([{"label": "cat"}])
    assert label == "cat"
    assert confidence is None


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"error": "Model is currently loading"},
        [{"score": 0.5}],
        [{"label": "cat", "score": "high"}],
        [{"label": "cat", "score": 1.5}],
        ["cat"],
        "not json",
    ],
)
def test_validate_prediction_rejects_malformed_payloads(payload):
    with pytest.raises(CloudInferenceError) as excinfo:
        validate_prediction(payload)
    assert excinfo.value.kind in {"invalid_response", "api_error"}


def test_labelled_prediction_is_scored_for_accuracy(settings, make_provider):
    result = send_image_to_cloud(
        settings.images_dir / "test_3.jpg",
        settings=settings,
        provider=make_provider([{"label": "banana", "score": 0.97}]),
        test_case="TC1.3",
    )
    result.expected_label = "banana"
    result.prediction_correct = labels_match(result.predicted_label, result.expected_label)

    summary = summarise([result.as_row()])
    assert summary.labelled == 1
    assert summary.correct == 1
    assert summary.accuracy == 100.0


def test_mismatched_prediction_is_scored_as_failure(settings, make_provider):
    result = send_image_to_cloud(
        settings.images_dir / "test_1.jpg",
        settings=settings,
        provider=make_provider([{"label": "golden retriever", "score": 0.8}]),
        test_case="TC1.3",
    )
    result.expected_label = "cat"
    result.prediction_correct = labels_match(result.predicted_label, result.expected_label)

    summary = summarise([result.as_row()])
    assert summary.labelled == 1
    assert summary.correct == 0
    assert summary.accuracy == 0.0
