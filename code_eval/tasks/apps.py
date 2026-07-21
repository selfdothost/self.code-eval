"""Measuring Coding Challenge Competence With APPS
https://arxiv.org/abs/2105.09938

APPS is a benchmark for code generation with 10000 problems. With three difficulty levels: introductory, interview and competition.
It can be used to evaluate the ability of language models to generate code from natural language specifications.

Homepage: https://github.com/hendrycks/apps
"""

import json

from code_eval.base import Task
from code_eval.tasks.custom_metrics.apps_metric import evaluate_generations, get_results

_CITATION = """
@article{hendrycksapps2021,
  title={Measuring Coding Challenge Competence With APPS},
  author={Dan Hendrycks and Steven Basart and Saurav Kadavath and Mantas Mazeika and Akul Arora and Ethan Guo and Collin Burns and Samir Puranik and Horace He and Dawn Song and Jacob Steinhardt},
  journal={NeurIPS},
  year={2021}
}
"""


LEVELS = ["introductory", "interview", "competition"]


def create_all_tasks():
    """Creates a dictionary of tasks from a list of levels
    :return: {task_name: task}
        e.g. {apps-interview: Task, apps-competitoon: Task}
    """
    return {f"apps-{level}": create_task(level) for level in LEVELS}


def create_task(level):
    class APPS(GeneralAPPS):
        def __init__(self, **kwargs):
            super().__init__(level, **kwargs)

    return APPS


class GeneralAPPS(Task):
    """A task represents an entire benchmark including its dataset, problems,
    answers, generation settings and evaluation methods.
    """

    DATASET_PATH = "codeparrot/apps"
    DATASET_NAME = None

    def __init__(self, level, k_list=[1, 10, 100]):
        self.DATASET_NAME = level
        super().__init__(
            stop_words=["\nQUESTION", "\n---", "\nANSWER"],
            requires_execution=True,
        )
        self.k_list = k_list

    def get_dataset(self):
        """Returns dataset for the task or an iterable of any object, that get_prompt can handle"""
        return self.dataset["test"]

    def get_prompt(self, doc):
        """Generate prompts for APPS
        Finetuning setup: prompt=question  with some starter code and function name if they exist.
        We also specify the type of the prompt, i.e. whether it is call-based or standard input-based.
        """
        starter_code = None if len(doc["starter_code"]) == 0 else doc["starter_code"]
        try:
            input_outpout = json.loads(doc["input_output"])
            fn_name = (
                None if not input_outpout.get("fn_name") else input_outpout["fn_name"]
            )
        except ValueError:
            fn_name = None
        prompt = "\nQUESTION:\n"
        prompt += doc["question"]
        if starter_code:
            prompt += starter_code
        if not fn_name:
            call_format = "\nUse Standard Input format"
            prompt += call_format
        else:
            call_format = "\nUse Call-Based format"
            prompt += call_format
        prompt += "\nANSWER:\n"
        return prompt

    def get_reference(self, doc):
        """Builds the reference solution for the doc (sample from the test dataset)."""
        return None

    def postprocess_generation(self, generation, idx):
        """Defines the postprocessing for a LM generation.
        :param generation: str
            code generation from LM
        :param idx: int
            index of doc in the dataset to which the generation belongs
            (not used for APPS)
        """
        try:
            generation = generation.split("\nANSWER:", 1)[1]
        except IndexError:
            # happens when prompts were very long and got truncated
            pass
        return generation

    def process_results(self, generations, references):
        """Takes the list of LM generations and evaluates them against ground truth references,
        returning the metric for the generations.
        :param generations: list(list(str))
            list of lists containing generations
        :param references: list(str)
            list of str containing refrences (not needed for APPS Task)

        Scoring used to go through `evaluate.load("codeparrot/apps_metric")`,
        which fetches that metric's `_compute()` implementation live from the
        HF Hub at runtime and was called with a `results_details=True` kwarg
        it doesn't accept -- and never has, at any point in its git history.
        See code_eval/tasks/custom_metrics/apps_metric/NOTICE for the full
        investigation (self.code-eval issue #2, item 6). We now vendor the
        metric's actual compute logic (custom_metrics/apps_metric/) and call
        it directly, building `details` from the real raw per-problem,
        per-generation results instead of the `results_details`/`"results"`
        shape that was never actually returned upstream.
        """
        raw_results = evaluate_generations(
            generations, level=self.DATASET_NAME, debug=False
        )
        results = get_results(raw_results, count_errors=True, k_list=self.k_list)

        # Build details from the real per-problem, per-generation results.
        # raw_results is {problem_index: [[test_case_result, ...], ...]} --
        # one inner list per generation, one entry per test case; entries
        # are True/False, or the sentinel ints -2 (compile error) / -1
        # (runtime error). A generation "passed" iff every test case it ran
        # against came back True.
        details = {}
        for task_id, generation_results in raw_results.items():
            task_details = []
            for comp_id, test_case_results in enumerate(generation_results):
                passed = len(test_case_results) > 0 and all(
                    r is True for r in test_case_results
                )
                result_str = "passed" if passed else str(test_case_results)
                task_details.append((comp_id, {"passed": passed, "result": result_str}))
            details[task_id] = task_details
        results["details"] = details
        return results
