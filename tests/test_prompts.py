import json

from code_eval import tasks
from code_eval.tasks.humanevalpack import create_task

TASKS = ["pal-gsm8k-greedy"]

sample_doc = {"pal-gsm8k-greedy": {"question": "test"}}


def load_reference_prompt(task_name):
    with open(f"tests/data/{task_name}_prompt.json") as fp:
        prompts = json.load(fp)
    return prompts["prompt"]


def test_gsm_prompt():
    for task_name in TASKS:
        task = tasks.get_task(task_name)
        task_prompt = task.get_prompt(sample_doc[task_name])
        ref_prompt = load_reference_prompt(task_name)
        assert task_prompt == ref_prompt


def test_humanevalpack_default_prompt_is_valid():
    """Regression test for issue #2 item 2 (bug 1).

    HumanEvalPack's own class-level default is prompt="instruct" (see
    HumanEvalPack.__init__ and every concrete task class create_task() builds in
    code_eval/tasks/humanevalpack.py). main.py's --prompt CLI flag used to default
    to the placeholder literal "prompt", which humanevalpack.py's get_prompt()
    rejects with a ValueError since it isn't one of the supported prompt modes.
    Instantiating any humanevalpack family task with its own default prompt value
    (mirroring the corrected CLI default) must not raise.
    """
    doc = {
        "prompt": "def add(a, b):\n",
        "declaration": "def add(a, b):\n",
        "instruction": "Write a function that adds two numbers.",
        "entry_point": "add",
        "buggy_solution": "    return a - b\n",
        "test": "assert add(2, 3) == 5",
    }
    for mode in ["synthesize", "fixtests", "fixdocs"]:
        task_cls = create_task("python", mode)
        task = task_cls()  # uses the class's own default prompt="instruct"
        assert task.prompt == "instruct"
        prompt_text = task.get_prompt(doc)
        assert isinstance(prompt_text, str)


def test_main_prompt_cli_default_is_instruct(monkeypatch):
    """The --prompt CLI flag (main.py) must default to a value humanevalpack.py
    actually supports. It used to default to the literal string "prompt", which
    is not a supported prompt mode and made every humanevalpack task fail
    immediately (see test_humanevalpack_default_prompt_is_valid)."""
    import sys

    import main as main_module

    monkeypatch.setattr(sys, "argv", ["main.py"])
    args = main_module.parse_args()
    assert args.prompt == "instruct"
