"""Phase D, item 2: the startup-time guard that refuses to run under
more than one worker process (the UW concurrency gate is process-local,
confirmed -- see core/uw_concurrency.py)."""
from __future__ import annotations

import pytest

from app.core.single_process_guard import (
    MultiWorkerConfigurationError, assert_single_process, detected_worker_count,
)


def test_detects_single_worker_as_default_with_empty_env():
    assert detected_worker_count({}) == 1


def test_detects_web_concurrency_env_var():
    assert detected_worker_count({"WEB_CONCURRENCY": "4"}) == 4


def test_detects_uvicorn_workers_env_var():
    assert detected_worker_count({"UVICORN_WORKERS": "2"}) == 2


def test_detects_gunicorn_workers_env_var():
    assert detected_worker_count({"GUNICORN_WORKERS": "3"}) == 3


def test_detects_gunicorn_cmd_args_workers_flag():
    assert detected_worker_count({"GUNICORN_CMD_ARGS": "--workers 5 --timeout 30"}) == 5


def test_unparseable_gunicorn_cmd_args_workers_fails_safe_to_at_least_two():
    assert detected_worker_count({"GUNICORN_CMD_ARGS": "--workers notanumber"}) == 2


def test_web_concurrency_of_one_is_fine():
    assert detected_worker_count({"WEB_CONCURRENCY": "1"}) == 1


def test_malformed_env_value_ignored_not_crashed_on():
    assert detected_worker_count({"WEB_CONCURRENCY": "not-a-number"}) == 1


def test_assert_single_process_passes_with_no_multi_worker_env():
    assert_single_process({})  # must not raise


def test_assert_single_process_raises_with_web_concurrency_above_one():
    with pytest.raises(MultiWorkerConfigurationError):
        assert_single_process({"WEB_CONCURRENCY": "4"})


def test_assert_single_process_message_names_the_real_mechanism():
    with pytest.raises(MultiWorkerConfigurationError) as exc_info:
        assert_single_process({"UVICORN_WORKERS": "2"})
    assert "process-local" in str(exc_info.value)


def test_dockerfile_cmd_has_no_workers_flag():
    """CONFIRMED this phase (Chad's STEP 0 instruction): the real
    Dockerfile's CMD is plain uvicorn with no --workers flag. This test
    guards against someone adding one later without also addressing the
    process-local concurrency gate (see core/uw_concurrency.py /
    core/single_process_guard.py)."""
    from pathlib import Path

    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    cmd_lines = [line for line in text.splitlines() if line.strip().startswith("CMD")]
    assert cmd_lines, "no CMD line found in Dockerfile"
    assert "--workers" not in cmd_lines[-1], (
        "Dockerfile CMD now specifies --workers -- this requires re-auditing the UW concurrency "
        "guard for real cross-process safety (see core/single_process_guard.py's docstring) before "
        "this test can be safely updated to allow it."
    )
