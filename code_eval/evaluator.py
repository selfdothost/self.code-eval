import inspect
import json
import os
import warnings

from typing import List


from code_eval import tasks
from code_eval.generation import parallel_generations

_WARNING = """
################################################################################
                                  !!!WARNING!!!
################################################################################
The "code_eval"/"apps_metric" you are about to use, execute untrusted 
model-generated code in Python.
Although it is highly unlikely that model-generated code will do something
overtly malicious in response to this test suite, model-generated code may act
destructively due to a lack of model capability or alignment.
Users are strongly encouraged to sandbox this evaluation suite so that it
does not perform destructive actions on their host or network. For more
information on how OpenAI sandboxes its code, see the paper "Evaluating Large
Language Models Trained on Code" (https://arxiv.org/abs/2107.03374).
Once you have read this disclaimer and taken appropriate precautions, set the argument 
"allow_code_execution" to True.
################################################################################\
"""

def _prompt_to_text(prompt_contents, args):
    """Render whatever task.get_prompt() returned as flat text.

    Most tasks return a plain string. Instruction-style tasks (instruct-humaneval,
    instruct-humaneval-nocontext, instruct_wizard_humaneval) return a
    {"instruction", "context"} dict instead. Mirror the exact flattening that
    api_generation.py and code_eval/utils.py's TokenizedDataset already use to build
    the actual request/generation text, so prompt-stripping and the "prompt" field
    in the detail report reflect what the model actually saw, regardless of which
    generation path (API or local) produced the completions.

    Returns None for prompt shapes with no single meaningful flat-text form (e.g.
    FIM {"prefix", "suffix"} infill prompts) — callers should treat that as "can't
    determine, don't strip" rather than guessing.
    """
    if isinstance(prompt_contents, str):
        return prompt_contents
    if isinstance(prompt_contents, dict) and set(prompt_contents.keys()) == {"instruction", "context"}:
        instruction = prompt_contents["instruction"]
        context = prompt_contents["context"]
        instruction_tokens = getattr(args, "instruction_tokens", None)
        if instruction_tokens:
            tokens = instruction_tokens.split(",")
            user_token, end_token, assistant_token = tokens[0], tokens[1], tokens[2]
        else:
            user_token, end_token, assistant_token = "", "", "\n"
        prefix = getattr(args, "prefix", "") or ""
        return prefix + user_token + instruction + end_token + assistant_token + context
    return None


class Evaluator:
    def __init__(self, accelerator, model, tokenizer, args):
        self.accelerator = accelerator
        self.model = model
        self.tokenizer = tokenizer
        self.args = args

        # setup arguments
        self.metric_output_path = args.metric_output_path

        # code evaluation permission
        self.allow_code_execution = args.allow_code_execution

    def generate_text(self, task_name, intermediate_generations=None):
        task = tasks.get_task(task_name, self.args)
        dataset = task.get_dataset()
        # if args.limit is None, use all samples
        # if args.limit is used, make sure args.limit_start + args.limit <= len(dataset)
        n_tasks = min(self.args.limit, len(dataset) - self.args.limit_start) if self.args.limit else len(dataset)
        # when args.limit is None
        # adjust n_tasks by args.limit_start to prevent out of bounds issues 
        if not self.args.limit:
            n_tasks -= self.args.limit_start
        references = [task.get_reference(dataset[i]) for i in range(self.args.limit_start, self.args.limit_start+n_tasks)]

        if self.args.check_references:
            if "get_solution" in inspect.signature(task.get_reference).parameters:
                solutions = [[task.get_reference(dataset[i], get_solution=True)] for i in range(self.args.limit_start, self.args.limit_start+n_tasks)]
            else:
                solutions = [[ref] for ref in references]
            return solutions, references

        curr_generations = []  # list[list[str | None] | None]
        if intermediate_generations:
            curr_generations = [gen for gen in intermediate_generations if gen]
            n_tasks -= len(curr_generations)
        intermediate_save_generations_path = f"{os.path.splitext(self.args.save_generations_path)[0]}_{task_name}_intermediate.json"
        curr_sample_idx = len(curr_generations)

        if getattr(self.args, "api_endpoint", None):
            from code_eval.api_generation import api_parallel_generations

            generations = api_parallel_generations(
                task,
                dataset,
                api_endpoint=self.args.api_endpoint,
                n_tasks=n_tasks,
                args=self.args,
                curr_sample_idx=curr_sample_idx,
                save_every_k_tasks=self.args.save_every_k_tasks,
                intermediate_generations=curr_generations,
                intermediate_save_generations_path=intermediate_save_generations_path,
            )
        else:
            generations = parallel_generations(
                task,
                dataset,
                self.accelerator,
                self.model,
                self.tokenizer,
                n_tasks=n_tasks,
                args=self.args,
                curr_sample_idx=curr_sample_idx,  # curr_sample_idx will added to limit_start to fix indexing
                save_every_k_tasks=self.args.save_every_k_tasks,
                intermediate_generations=curr_generations,
                intermediate_save_generations_path=intermediate_save_generations_path,
            )

        if len(generations[0]) > self.args.n_samples:
            generations = [l[: self.args.n_samples] for l in generations]
            warnings.warn(
                f"Number of tasks wasn't proportional to number of devices, we removed extra predictions to only keep nsamples={self.args.n_samples}"
            )
        return generations, references

    def evaluate(self, task_name, intermediate_generations=None):
        task = tasks.get_task(task_name, self.args)
        if task.requires_execution and not self.allow_code_execution:
            raise ValueError(_WARNING)

        generations, references = self.generate_text(task_name, intermediate_generations=intermediate_generations)

        if self.accelerator.is_main_process:
            if not self.args.load_generations_path:
                save_generations_path = f"{os.path.splitext(self.args.save_generations_path)[0]}_{task_name}.json"
                self.save_json_files(generations, references, save_generations_path, f"references_{task_name}.json")

            # make sure tokenizer plays nice with multiprocessing
            os.environ["TOKENIZERS_PARALLELISM"] = "false"
            if self.allow_code_execution and task.requires_execution:
                os.environ["HF_ALLOW_CODE_EVAL"] = "1"
            print("Evaluating generations...")
            results = task.process_results(generations, references)

            # Save detailed per-problem report if requested
            details = results.pop("details", None)
            if details is not None and getattr(self.args, "save_details_path", None):
                dataset = task.get_dataset()
                n_tasks = len(generations)
                detailed_report = []
                for task_id in range(n_tasks):
                    dataset_idx = self.args.limit_start + task_id
                    doc = dataset[dataset_idx]
                    prompt_contents = task.get_prompt(doc)
                    # Some tasks (e.g. instruct-humaneval family) return a
                    # {"instruction", "context"} dict rather than a plain string.
                    # Flatten it the same way the generation paths do before
                    # comparing against/stripping from the raw generation.
                    prompt = _prompt_to_text(prompt_contents, self.args)
                    task_results = details.get(task_id, [])
                    # Sort by completion_id
                    task_results.sort(key=lambda x: x[0])
                    samples = []
                    for completion_id, result_info in task_results:
                        gen = generations[task_id][completion_id] if completion_id < len(generations[task_id]) else ""
                        # Strip the prompt to show only what the model generated.
                        # If we couldn't derive a flat prompt string (e.g. FIM
                        # infill prompts), leave the generation unstripped rather
                        # than guess.
                        completion = gen[len(prompt):] if prompt is not None and gen.startswith(prompt) else gen
                        samples.append({
                            "completion_id": completion_id,
                            "generation": gen,
                            "completion": completion,
                            "passed": result_info["passed"],
                            "result": result_info["result"],
                        })
                    # Support both dict-like objects and objects with subscript access only
                    def _doc_get(d, key, default=""):
                        try:
                            return d.get(key, default)
                        except AttributeError:
                            try:
                                return d[key]
                            except (KeyError, IndexError, TypeError):
                                return default

                    entry = {
                        "task_id": _doc_get(doc, "task_id", f"task_{task_id}"),
                        "prompt": prompt if prompt is not None else prompt_contents,
                        "entry_point": _doc_get(doc, "entry_point"),
                        "canonical_solution": _doc_get(doc, "canonical_solution"),
                        "reference_test": references[task_id],
                        "samples": samples,
                    }
                    detailed_report.append(entry)

                from datetime import datetime
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                details_path = f"{os.path.splitext(self.args.save_details_path)[0]}_{task_name}_{timestamp}.json"
                with open(details_path, "w") as fp:
                    json.dump(detailed_report, fp, indent=2)
                    print(f"detailed results saved at {details_path}")

            return results

    def save_json_files(
        self,
        generations: List[str],
        references: List[str],
        save_generations_path: str,
        save_references_path: str,
    ) -> None:
        if self.args.save_generations:
            with open(save_generations_path, "w") as fp:
                json.dump(generations, fp)
                print(f"generations were saved at {save_generations_path}")
        if self.args.save_references:
            with open(save_references_path, "w") as fp:
                json.dump(references, fp)
                print(f"references were saved at {save_references_path}")
