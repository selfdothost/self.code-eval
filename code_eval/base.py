import os
from abc import ABC, abstractmethod
from warnings import warn

from datasets import load_dataset


def _hf_hub_token():
    """Resolve an HF Hub token for gated dataset access, if one is configured.

    Reads the same environment variables huggingface_hub's own
    `login()`/`get_token()` resolve, in the same priority order: `HF_TOKEN`
    first, `HUGGING_FACE_HUB_TOKEN` kept for backward compatibility. Neither
    is exported anywhere in this repo by default (no CLI arg reaches this
    deep, see `main.py`'s `--use_auth_token`, which only threads into the
    local model-loading path) -- set one at deploy time via the yard's
    secrets mechanism to authenticate gated dataset downloads (e.g.
    `studenteval`).

    Returns `None` when neither is set, which `datasets.load_dataset`'s
    `token=` treats identically to the argument being omitted entirely
    (anonymous access) -- i.e. no behavior change for ungated datasets.
    """
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")


class Task(ABC):
    """A task represents an entire benchmark including its dataset, problems,
    answers, generation settings and evaluation methods.
    """

    # The name of the `Task` benchmark as denoted in the HuggingFace datasets Hub
    DATASET_PATH: str = None

    # The name of a subset within `DATASET_PATH`.
    DATASET_NAME: str = None

    # Set to True only by tasks that are genuinely local-only and do not load
    # `self.dataset` via the HF Hub at all (e.g. DS-1000, which leaves
    # `DATASET_PATH`/`DATASET_NAME` unset and fetches its own data separately
    # in `_download_dataset`). For those tasks, the `load_dataset(...)` call
    # below is expected to fail on every construction -- not just under a
    # network hiccup -- so the failure is caught and warned instead of
    # raised. Every other task depends on `self.dataset` actually being
    # populated, so a failed load must raise immediately rather than leave
    # `self.dataset` unset and defer the failure to a confusing
    # `AttributeError` at generation time.
    DATASET_LOAD_OPTIONAL: bool = False

    def __init__(self, stop_words=None, requires_execution=True):
        """
        :param stop_words: list
            list of stop words if the generation uses a stopping criteria during generation
        :param requires_execution: bool
            wheter the task requires code execution during evaluation or not
        """
        self.stop_words = stop_words
        self.requires_execution = requires_execution
        try:
            self.dataset = load_dataset(
                path=self.DATASET_PATH,
                name=self.DATASET_NAME,
                trust_remote_code=True,
                token=_hf_hub_token(),
            )
        except Exception as e:
            if not self.DATASET_LOAD_OPTIONAL:
                raise
            warn(
                f"Loading the dataset failed with {str(e)}. This task will use a locally downloaded dataset, not from the HF hub. \
                This is expected behavior for the DS-1000 benchmark but not for other benchmarks!"
            )

    @abstractmethod
    def get_dataset(self):
        """Returns dataset for the task or an iterable of any object, that get_prompt can handle"""
        return []

    def fewshot_examples(self):
        """Loads and returns the few-shot examples for the task if they exist."""
        pass

    @abstractmethod
    def get_prompt(self, doc):
        """Builds the prompt for the LM to generate from.
        :param doc: dict[str: str]
            sample from the test dataset
        """
        pass

    @abstractmethod
    def get_reference(self, doc):
        """Builds the reference solution for the doc.
        :param doc: dict[str: str]
            sample from the test dataset
        """
        pass

    @abstractmethod
    def postprocess_generation(self, generation, idx):
        """Defines the postprocessing for a LM generation.
        :param generation: str
            code generation from LM
        :param idx: int
            index of doc in the dataset to which the generation belongs
        """
        pass

    @abstractmethod
    def process_results(self, generations, references):
        """Takes the list of LM generations and evaluates them against ground truth references,
        returning the metric for the generations as in {"metric_name": result}.
        :param generations: list(list(str))
            list of lists containing generations
        :param references: list(str)
            list of str containing refrences
        :return: dict[str: float]
        """
        pass

    @staticmethod
    def _stop_at_stop_token(decoded_string, stop_tokens):
        """
        Produces the prefix of decoded_string that ends at the first occurrence of
        a stop_token.
        WARNING: the decoded_string *must not* include the prompt, which may have stop tokens
        itself.
        """
        min_stop_index = len(decoded_string)
        for stop_token in stop_tokens:
            stop_index = decoded_string.find(stop_token)
            if stop_index != -1 and stop_index < min_stop_index:
                min_stop_index = stop_index
        return decoded_string[:min_stop_index]
