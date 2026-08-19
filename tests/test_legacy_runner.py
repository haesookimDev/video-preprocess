"""Compatibility checks for the legacy sequential pipeline runner."""

from pipeline.runner import STAGES


def test_legacy_runner_keeps_embedded_text_before_timeline() -> None:
    names = tuple(stage.NAME for stage in STAGES)

    assert len(names) == 13
    assert names.index("04_audio") < names.index("04_embedded_text")
    assert names.index("04_embedded_text") < names.index("09_timeline")
