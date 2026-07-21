"""Regression tests for issue #2 item 2 (bug 2): the detail-report path in
code_eval/evaluator.py crashed with `TypeError: startswith first arg must be
str... not dict` for tasks whose get_prompt(doc) returns a
{"instruction", "context"} dict (instruct-humaneval, instruct-humaneval-nocontext)
instead of a plain string. This only surfaced when --save_details_path was set,
which self-code-eval's job API always passes.
"""

from types import SimpleNamespace

from code_eval.evaluator import _prompt_to_text
from code_eval.tasks.instruct_humaneval import InstructHumanEvalWithContext


class _Args(SimpleNamespace):
    """Minimal stand-in for the argparse Namespace evaluator.py reads from."""


def _args(prefix="", instruction_tokens=None):
    return _Args(prefix=prefix, instruction_tokens=instruction_tokens)


def test_prompt_to_text_passthrough_for_string_prompts():
    """Plain string prompts (the common case) must be returned unchanged."""
    assert _prompt_to_text("def foo():\n    pass\n", _args()) == "def foo():\n    pass\n"


def test_prompt_to_text_flattens_instruction_context_dict():
    """Dict-shaped {"instruction", "context"} prompts must flatten to the exact
    same text api_generation.py builds and sends to the model, so that stripping
    the prompt from a generation (and the report's "prompt" field) reflects what
    the model actually saw."""
    prompt_contents = {"instruction": "Write add(a, b).", "context": "def add(a, b):\n"}
    flattened = _prompt_to_text(prompt_contents, _args())
    # Default tokens (no --instruction_tokens set): user/end tokens empty, "\n" between.
    assert flattened == "Write add(a, b).\ndef add(a, b):\n"


def test_prompt_to_text_flattens_with_prefix_and_instruction_tokens():
    prompt_contents = {"instruction": "Write add(a, b).", "context": "def add(a, b):\n"}
    args = _args(prefix="<PFX>", instruction_tokens="<user>,<end>,<assistant>")
    flattened = _prompt_to_text(prompt_contents, args)
    assert flattened == "<PFX><user>Write add(a, b).<end><assistant>def add(a, b):\n"


def test_prompt_to_text_returns_none_for_infill_dicts():
    """FIM {"prefix", "suffix"} prompts have no single meaningful flat-text form;
    callers must treat that as "can't determine, don't strip" instead of guessing."""
    assert _prompt_to_text({"prefix": "a", "suffix": "b"}, _args()) is None


def test_instruct_humaneval_get_prompt_is_a_dict_not_a_string():
    """Confirms the actual bug precondition: instruct-humaneval's get_prompt()
    really does return a dict, not a string."""
    task = InstructHumanEvalWithContext()
    doc = {"instruction": "Write add(a, b).", "context": "def add(a, b):\n"}
    prompt_contents = task.get_prompt(doc)
    assert isinstance(prompt_contents, dict)
    assert set(prompt_contents.keys()) == {"instruction", "context"}


def test_detail_report_prompt_stripping_does_not_crash_and_strips_correctly():
    """Reproduces the exact detail-report logic from evaluator.py's evaluate()
    method for a dict-shaped-prompt task. Before the fix, `gen.startswith(prompt)`
    raised TypeError because `prompt` was still a dict. After the fix, `gen`
    (which api_generation.py builds as `flattened_prompt + generated_text`) must
    be correctly recognized as prompt-prefixed and stripped down to just the
    generated continuation.
    """
    task = InstructHumanEvalWithContext()
    doc = {"instruction": "Write add(a, b).", "context": "def add(a, b):\n"}
    prompt_contents = task.get_prompt(doc)

    args = _args()
    prompt = _prompt_to_text(prompt_contents, args)
    assert prompt is not None

    generated_text = "    return a + b\n"
    # Mirrors api_generation.py: full_text = prompt + gen_text
    gen = prompt + generated_text

    # This is the exact expression evaluator.py's evaluate() uses; it must not
    # raise TypeError for a dict-shaped prompt_contents anymore.
    completion = gen[len(prompt):] if prompt is not None and gen.startswith(prompt) else gen

    assert completion == generated_text


def test_detail_report_prompt_stripping_falls_back_gracefully_for_infill():
    """For prompt shapes with no flat-text form (FIM infill), the report must not
    crash and must leave the generation unstripped rather than mis-strip it."""
    prompt_contents = {"prefix": "def add(a, b):\n", "suffix": "\n"}
    args = _args()
    prompt = _prompt_to_text(prompt_contents, args)
    assert prompt is None

    gen = "    return a + b\n"
    completion = gen[len(prompt):] if prompt is not None and gen.startswith(prompt) else gen
    assert completion == gen
