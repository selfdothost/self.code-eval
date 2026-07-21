"""Tests for the vendored codeparrot/apps_metric compute path.

self.code-eval issue #2, item 6: `code_eval/tasks/apps.py::process_results`
used to call `evaluate.load("codeparrot/apps_metric")`, which fetches that
metric's `_compute()` live from the HF Hub and was invoked with a
`results_details=True` kwarg it never actually accepted (see
code_eval/tasks/custom_metrics/apps_metric/NOTICE for the full
investigation). We now vendor the metric's compute logic and call it
directly.

These tests avoid the sandboxed code-execution boundary
(`custom_metrics/apps_metric/testing_util.py::run_test`, which compiles and
executes generated code in a subprocess) by stubbing it out or by feeding
`get_results`/`GeneralAPPS.process_results` pre-computed raw results instead
of real generations -- consistent with the fact that this repo's existing
evaluator tests (tests/test_generation_evaluation.py) exercise task wiring
against known-safe fixture generations rather than unit-testing the sandbox
itself.
"""

from unittest.mock import patch

from code_eval.tasks.apps import GeneralAPPS
from code_eval.tasks.custom_metrics.apps_metric.utils import (
    evaluate_generations,
    get_results,
)


def make_apps_task(level="introductory", k_list=(1,)):
    """Builds a GeneralAPPS instance without hitting Task.__init__'s
    load_dataset() call -- process_results() never touches self.dataset,
    so it's safe to bypass the network-fetching constructor entirely."""
    task = GeneralAPPS.__new__(GeneralAPPS)
    task.DATASET_NAME = level
    task.k_list = list(k_list)
    task.stop_words = ["\nQUESTION", "\n---", "\nANSWER"]
    task.requires_execution = True
    return task


# --- get_results(): pure aggregation logic, matches the vendored file's own
# doctest examples verbatim so we know the vendored copy behaves exactly
# like the upstream commit it was pinned from.

def test_get_results_single_generation_accuracy():
    results = {
        0: [[-2]],
        1: [[False, False]],
        2: [[True, True]],
        3: [[False, True, False, True]],
        4: [[-1, -1]],
    }
    metrics = get_results(results, count_errors=True)
    assert metrics["avg_accuracy"] == 0.3
    assert metrics["strict_accuracy"] == 0.2
    assert metrics["pass_at_k"] is None


def test_get_results_multiple_generations_pass_at_k():
    results = {
        0: [[-2], [True, True, True]],
        1: [[-1, -1, -1], [True, False, True]],
    }
    metrics = get_results(results, k_list=[1, 2])
    assert metrics["avg_accuracy"] is None
    assert metrics["strict_accuracy"] is None
    assert metrics["pass_at_k"] == {"pass@1": 0.25, "pass@2": 0.5}


# --- evaluate_generations(): stub the sandboxed execution boundary
# (check_correctness -> run_test, which spawns a subprocess to compile and
# run generated code) and the APPS dataset load, so the test stays hermetic
# and fast while still exercising the real per-problem/per-generation
# results-shape logic (including the "stop appending generations for a
# problem after the first compile-error exception" behavior).

def test_evaluate_generations_stubs_sandbox_execution():
    fake_dataset = [
        {"input_output": "{}"},  # problem 0
        {"input_output": "{}"},  # problem 1
    ]

    def fake_check_correctness(sample, generation, timeout, debug=True):
        # generation text stands in for a canned sandbox result so we never
        # actually compile/exec anything here.
        if generation == "PASS":
            return [True, True]
        if generation == "FAIL":
            return [False, True]
        raise RuntimeError("boom")  # simulates a compile error path

    with patch(
        "code_eval.tasks.custom_metrics.apps_metric.utils.load_dataset",
        return_value=fake_dataset,
    ), patch(
        "code_eval.tasks.custom_metrics.apps_metric.utils.check_correctness",
        side_effect=fake_check_correctness,
    ):
        generations = [["PASS", "FAIL"], ["boom"]]
        raw_results = evaluate_generations(generations, level="introductory")

    assert raw_results[0] == [[True, True], [False, True]]
    # the exception path appends the default [-2] sentinel and stops
    # evaluating further generations for that problem (upstream behavior).
    assert raw_results[1] == [[-2]]


# --- GeneralAPPS.process_results(): the actual call site fixed in this
# change. Mock evaluate_generations (the sandbox-execution entry point) and
# verify process_results wires it to get_results() and builds `details`
# from the real raw per-problem/per-generation shape -- not the
# `results_details=True` / `"results"` shape the old, broken call site
# expected.

def test_process_results_builds_details_from_raw_results():
    task = make_apps_task(level="introductory", k_list=[1])

    raw_results = {
        0: [[True, True]],           # single generation, passed
        1: [[False, True]],          # single generation, failed a test case
        2: [[-2]],                   # single generation, compile error
    }

    with patch(
        "code_eval.tasks.apps.evaluate_generations", return_value=raw_results
    ) as mock_eval_gens:
        results = task.process_results(generations=[["g0"], ["g1"], ["g2"]], references=[None, None, None])

    mock_eval_gens.assert_called_once_with(
        [["g0"], ["g1"], ["g2"]], level="introductory", debug=False
    )

    # aggregate metrics still come from the real vendored get_results()
    assert results["avg_accuracy"] is not None
    assert results["strict_accuracy"] is not None

    # details built from the real raw shape, keyed by problem index
    assert results["details"][0] == [(0, {"passed": True, "result": "passed"})]
    assert results["details"][1] == [(0, {"passed": False, "result": "[False, True]"})]
    assert results["details"][2] == [(0, {"passed": False, "result": "[-2]"})]


def test_process_results_no_longer_requests_results_details_kwarg():
    """Regression guard: the old call site passed results_details=True to
    evaluate.load("codeparrot/apps_metric").compute(...), a kwarg that
    metric's _compute() has never accepted (see NOTICE). Assert the vendored
    call path takes no such kwarg."""
    task = make_apps_task(level="introductory", k_list=[1])
    raw_results = {0: [[True]]}

    with patch(
        "code_eval.tasks.apps.evaluate_generations", return_value=raw_results
    ) as mock_eval_gens, patch(
        "code_eval.tasks.apps.get_results", wraps=get_results
    ) as mock_get_results:
        task.process_results(generations=[["g0"]], references=[None])

    # evaluate_generations/get_results are called with the documented
    # predictions/k_list/level surface only -- no results_details anywhere.
    for call in (mock_eval_gens, mock_get_results):
        for kwargs in (call.call_args.kwargs,):
            assert "results_details" not in kwargs
