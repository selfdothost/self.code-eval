"""Unit tests for code_eval.tasks.custom_metrics.beyond_eval.compute_beyond_eval.

These tests monkeypatch Sandbox.run_sample so no code is actually executed in a
sandboxed subprocess - we only exercise the aggregation/scoring logic of
compute_beyond_eval itself, keyed off the (fake) "solution" strings.

Covers self.code-eval issue #2, item 5: an instance whose reference solutions
all fail in the sandbox must be skipped (logged + excluded from the aggregate)
instead of crashing on min()/max() of an empty runtimes list.
"""
import json

import pytest

from code_eval.tasks.custom_metrics.beyond_eval import Sandbox, compute_beyond_eval

# solution "code" strings double as lookup keys into a canned sandbox result table.
# Each entry maps a solution string -> (result, runtime).
SANDBOX_TABLE = {
    # Instance A: two reference solutions, both pass.
    "A_ref_fast": ("passed", 1.0),
    "A_ref_slow": ("passed", 3.0),
    "A_gen_mid": ("passed", 2.0),
    "A_gen_fail": ("failed@cases", 0.0),
    # Instance B: reference solutions all fail in the sandbox (item 5 case).
    "B_ref_broken_1": ("failed@cases", 0.0),
    "B_ref_broken_2": ("failed@error", 0.0),
    "B_gen_anything": ("passed", 1.0),
    # Instance C: like A, to prove the aggregate still makes sense with a skip
    # sandwiched between two healthy instances.
    "C_ref_fast": ("passed", 0.5),
    "C_ref_slow": ("passed", 1.5),
    "C_gen_mid": ("passed", 1.0),
}


def _fake_run_sample(sample):
    result, runtime = SANDBOX_TABLE[sample["solution"]]
    return dict(
        result=result,
        runtime=runtime,
        index=sample["solution_index"],
        error="None" if result == "passed" else "canned failure",
    )


@pytest.fixture(autouse=True)
def _patch_sandbox(monkeypatch):
    # run_sample is a @staticmethod; rebinding the class attribute is enough for
    # `sandbox.run_sample(sample)` calls on any Sandbox() instance to hit the fake.
    monkeypatch.setattr(Sandbox, "run_sample", staticmethod(_fake_run_sample))


def _instance(task_id, difficulty, ref_solutions, entry_point="solve"):
    return {
        "task_id": task_id,
        "difficulty": difficulty,
        "entry_point": entry_point,
        "convert_offline": "",
        "evaluate_offline": "",
        "test_cases": json.dumps([]),
        "solutions": [{"solution": s} for s in ref_solutions],
    }


def test_normal_instance_computes_pass_and_beyond_scores():
    """Baseline case: an instance whose reference solutions all pass is scored
    exactly as before - unaffected by the empty-runtimes skip logic."""
    instance_a = _instance("A", "Easy", ["A_ref_fast", "A_ref_slow"])
    generations_a = ["A_gen_mid", "A_gen_fail"]

    results = compute_beyond_eval([generations_a], [instance_a])

    assert results["skipped_instances"] == []
    # 1 of 2 generations passed.
    assert results["Easy"]["passed"] == 1
    assert results["Easy"]["failed@cases"] == 1
    assert results["Easy_pass@1"] == pytest.approx(0.5)
    # A_gen_mid runtime=2.0 sits exactly mid-range between min=1.0/max=3.0 -> beyond=0.5
    # for the first generation; beyond@1 only looks at each instance's first
    # generation, averaged over instances (1 instance here) -> 0.5.
    assert results["Easy_beyond@1"] == pytest.approx(0.5)


def test_instance_with_no_passing_reference_solutions_is_skipped_cleanly():
    """Item 5: zero reference solutions pass in the sandbox -> skip the instance,
    log a warning identifying it, and do not crash."""
    instance_b = _instance("B", "Medium", ["B_ref_broken_1", "B_ref_broken_2"])
    generations_b = ["B_gen_anything"]

    with pytest.warns(UserWarning, match=r"skipping instance 'B'"):
        results = compute_beyond_eval([generations_b], [instance_b])

    assert results["skipped_instances"] == [{"index": 0, "task_id": "B"}]
    # No Medium generations were ever evaluated (the instance never reached the
    # generation-scoring loop), so error counters for Medium stay at zero and no
    # Medium pass@/beyond@ keys are produced.
    assert results["Medium"] == {
        "failed@load": 0, "failed@eval": 0, "failed@cases": 0,
        "failed@timeout": 0, "failed@error": 0, "passed": 0,
    }
    assert not any(k.startswith("Medium_pass@") or k.startswith("Medium_beyond@") for k in results)
    # Skipping affects Average too - no per-instance contribution was recorded,
    # so there's nothing to average and no Average_pass@/beyond@ keys appear
    # (there is no plain "Average" error-count key; only Easy/Medium/Hard get one).
    assert not any(k.startswith("Average_pass@") or k.startswith("Average_beyond@") for k in results)


def test_aggregate_excludes_skipped_instance_among_several():
    """One skipped instance sandwiched between two healthy ones: the aggregate
    output keeps its normal shape (dict of pass@k/beyond@k/error keys +
    skipped_instances) and reflects only the two scored instances."""
    instance_a = _instance("A", "Easy", ["A_ref_fast", "A_ref_slow"])
    instance_b = _instance("B", "Easy", ["B_ref_broken_1", "B_ref_broken_2"])
    instance_c = _instance("C", "Easy", ["C_ref_fast", "C_ref_slow"])

    generations = [["A_gen_mid"], ["B_gen_anything"], ["C_gen_mid"]]
    instances = [instance_a, instance_b, instance_c]

    with pytest.warns(UserWarning, match=r"skipping instance 'B'"):
        results = compute_beyond_eval(generations, instances)

    assert results["skipped_instances"] == [{"index": 1, "task_id": "B"}]
    # Only A and C contributed a generation each, and both passed.
    assert results["Easy"]["passed"] == 2
    assert results["Easy_pass@1"] == pytest.approx(1.0)
    # A_gen_mid -> beyond 0.5 (see test above); C_gen_mid runtime=1.0 sits exactly
    # mid-range between min=0.5/max=1.5 -> beyond=0.5 too.
    assert results["Easy_beyond@1"] == pytest.approx(0.5)
    # The Average bucket also reflects only the two non-skipped instances.
    assert results["Average_pass@1"] == pytest.approx(1.0)
    assert results["Average_beyond@1"] == pytest.approx(0.5)
    # Aggregate is still a well-formed results dict - no crash, no missing keys.
    assert set(results.keys()) >= {
        "Easy_pass@1", "Easy_beyond@1", "Average_pass@1", "Average_beyond@1",
        "Easy", "Medium", "Hard", "skipped_instances",
    }


def test_all_instances_of_a_difficulty_skipped_does_not_crash():
    """Defensive edge case: if every instance of a difficulty is skipped, the
    difficulty's pass@k/beyond@k keys are simply absent (nothing to average)
    instead of raising ZeroDivisionError."""
    instance_b = _instance("B", "Hard", ["B_ref_broken_1", "B_ref_broken_2"])

    with pytest.warns(UserWarning):
        results = compute_beyond_eval([["B_gen_anything"]], [instance_b])

    assert results["skipped_instances"] == [{"index": 0, "task_id": "B"}]
    assert not any(k.startswith("Hard_pass@") or k.startswith("Hard_beyond@") for k in results)
