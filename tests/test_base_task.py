"""Covers issue #2, item 4: `Task.__init__`'s dataset load must raise clearly
for any task that genuinely needs a dataset, instead of silently leaving
`self.dataset` unset and deferring the failure to a confusing
`AttributeError` at generation time. The old bare `try/except Exception:
warn()` was written "for DS-1000" but applied to every task; only DS-1000
(via `GeneralDS1000.DATASET_LOAD_OPTIONAL = True`) should keep the silent
catch-and-warn behavior.
"""

import importlib.util
import pathlib
from unittest.mock import patch

import pytest

from code_eval.base import Task


def _load_ds1000_module():
    """Load `code_eval/tasks/ds1000.py` directly, bypassing
    `code_eval/tasks/__init__.py` (which eagerly imports every task,
    including ones that require `torch`). `ds1000.py` itself only needs
    `code_eval.base`, `requests`, and `tqdm` at import time, so this keeps
    the test independent of the full (heavy) task registry.
    """
    ds1000_path = pathlib.Path(__file__).resolve().parents[1] / "code_eval" / "tasks" / "ds1000.py"
    spec = importlib.util.spec_from_file_location("code_eval.tasks.ds1000", ds1000_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GeneralDS1000 = _load_ds1000_module().GeneralDS1000


class _ConcreteTask(Task):
    """Minimal concrete `Task` subclass so `Task.__init__` can be exercised
    directly without pulling in a real benchmark's dataset/scoring logic.
    """

    def get_dataset(self):
        return []

    def get_prompt(self, doc):
        return ""

    def get_reference(self, doc):
        return ""

    def postprocess_generation(self, generation, idx):
        return generation

    def process_results(self, generations, references):
        return {}


class _LocalOnlyConcreteTask(_ConcreteTask):
    """Stand-in for a task that explicitly opts out of the network-dependent
    load, the way DS-1000 does."""

    DATASET_LOAD_OPTIONAL = True


def test_dataset_load_failure_reraises_by_default():
    """A task with a genuinely broken/missing dataset path (the common case
    -- gsm8k, DS-1000-adjacent, anything on the HF hub) must raise the real
    exception immediately at construction time, not swallow it."""
    with patch("code_eval.base.load_dataset", side_effect=RuntimeError("boom: dataset unreachable")):
        with pytest.raises(RuntimeError, match="boom: dataset unreachable"):
            _ConcreteTask()


def test_dataset_load_optional_task_still_swallows_failure():
    """A task that explicitly marks itself local-only keeps the previous
    silent-catch-and-warn behavior -- no regression for that opt-out path."""
    with patch("code_eval.base.load_dataset", side_effect=RuntimeError("boom: dataset unreachable")):
        with pytest.warns(UserWarning, match="boom: dataset unreachable"):
            task = _LocalOnlyConcreteTask()
    assert not hasattr(task, "dataset")


def test_dataset_load_optional_defaults_to_false():
    """The opt-out is off by default -- only tasks that explicitly set it
    keep the silent-catch behavior."""
    assert Task.DATASET_LOAD_OPTIONAL is False
    assert _ConcreteTask.DATASET_LOAD_OPTIONAL is False


def test_ds1000_is_the_task_marked_dataset_load_optional():
    """Locks in the actual investigation finding: DS-1000 does *not* bypass
    `Task.__init__`'s `load_dataset` call (it calls `super().__init__()` and
    goes through it like every other task) -- it just never sets
    `DATASET_PATH`/`DATASET_NAME`, so that call deterministically raises a
    `TypeError` on every construction, network or no. DS-1000 fetches its
    real data separately (`_download_source`/`_download_dataset`) and never
    reads `self.dataset`, so it needs the silent catch to survive
    construction at all -- the old comment was accurate about DS-1000
    itself, just misleadingly worded as if it covered every task.
    """
    assert GeneralDS1000.DATASET_LOAD_OPTIONAL is True
    assert GeneralDS1000.DATASET_PATH is None
