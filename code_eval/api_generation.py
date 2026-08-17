import json
import os
import warnings
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from tqdm import tqdm


class AllGenerationsFailedError(RuntimeError):
    """Every generation request failed, so there is nothing to score (#10).

    Raised instead of returning a list of empty strings. Empty generations score
    as failures, which produces a plausible-looking `pass@1: 0.0` for a run in
    which the model was never reached — indistinguishable, from the results
    file alone, from a model that genuinely solved nothing.
    """


class GenerationHealth:
    """Whether the *infrastructure* delivered generations, separate from whether
    the model got them right (#10).

    The distinction this exists to preserve: an empty generation is not evidence
    about the model unless the request that should have produced it succeeded. A
    403, a connection reset and a model that returned nothing all look identical
    downstream — every one of them scores as a failed sample.

    Reasons are recorded as HTTP status when there is one and the exception class
    otherwise, because that is the line between "the endpoint refused us"
    (auth, quota, a bad model name) and "we never reached it" (DNS, timeout).
    """

    def __init__(self) -> None:
        self.attempted = 0
        self.failed = 0
        self.reasons: Counter = Counter()

    def record_success(self) -> None:
        self.attempted += 1

    def record_failure(self, exc: BaseException) -> None:
        self.attempted += 1
        self.failed += 1
        self.reasons[self._classify(exc)] += 1

    @staticmethod
    def _classify(exc: BaseException) -> str:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is not None:
            return f"http_{status}"
        return type(exc).__name__

    @property
    def all_failed(self) -> bool:
        """True only when something was attempted and none of it worked.

        `attempted == 0` is deliberately NOT "all failed" — a task with no
        problems left to generate (everything resumed from an intermediate file)
        must not be mistaken for a total outage.
        """
        return self.attempted > 0 and self.failed == self.attempted

    def as_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "failed": self.failed,
            "succeeded": self.attempted - self.failed,
            "failure_rate": (self.failed / self.attempted) if self.attempted else 0.0,
            "reasons": dict(self.reasons),
        }


def _is_chat_endpoint(api_endpoint: str) -> bool:
    """Detect whether the endpoint is a chat completions or text completions API."""
    return "chat/completions" in api_endpoint


def api_parallel_generations(
    task,
    dataset,
    api_endpoint,
    n_tasks,
    args,
    curr_sample_idx: int = 0,
    save_every_k_tasks: int = -1,
    intermediate_generations: Optional[List[Optional[List[Optional[str]]]]] = None,
    intermediate_save_generations_path: Optional[str] = None,
    health: Optional[GenerationHealth] = None,
):
    """Generate completions via OpenAI-compatible API (chat or text completions).

    Pass ``health`` to receive per-request delivery accounting (#10); it is
    filled in place so the caller can report it alongside the score. Raises
    :class:`AllGenerationsFailedError` when every request failed — see that
    class for why returning empty generations instead is not acceptable.
    """
    if args.load_generations_path:
        with open(args.load_generations_path) as fp:
            generations = json.load(fp)
            print(
                f"generations loaded, {n_tasks} selected from {len(generations)} with {len(generations[0])} candidates"
            )
        return generations[:n_tasks]

    if health is None:
        health = GenerationHealth()

    generations = [] if not intermediate_generations else list(intermediate_generations)
    limit_start = args.limit_start + curr_sample_idx
    is_chat = _is_chat_endpoint(api_endpoint)

    # Build request headers
    headers = {"Content-Type": "application/json"}
    if getattr(args, "api_key", None):
        headers["Authorization"] = f"Bearer {args.api_key}"

    session = requests.Session()
    session.headers.update(headers)

    stop_words = list(task.stop_words) if task.stop_words else []
    # OpenAI API typically limits stop to 4 entries
    api_stop = stop_words[:4] if stop_words else None

    print(f"number of problems for this task is {n_tasks}")
    print(f"Generating via API: {api_endpoint} ({'chat' if is_chat else 'completions'} mode)")

    for sample_idx in tqdm(range(n_tasks), desc="API generation"):
        dataset_idx = limit_start + sample_idx
        prompt_contents = task.get_prompt(dataset[dataset_idx])

        # Build the text prompt (mirrors TokenizedDataset logic)
        if isinstance(prompt_contents, str):
            prompt = args.prefix + prompt_contents
        elif isinstance(prompt_contents, dict):
            if set(prompt_contents.keys()) == {"instruction", "context"}:
                instruction = prompt_contents["instruction"]
                context = prompt_contents["context"]
                if args.instruction_tokens:
                    tokens = args.instruction_tokens.split(",")
                    user_token, end_token, assistant_token = tokens[0], tokens[1], tokens[2]
                else:
                    user_token, end_token, assistant_token = "", "", "\n"
                prompt = args.prefix + user_token + instruction + end_token + assistant_token + context
            elif set(prompt_contents.keys()) == {"prefix", "suffix"}:
                raise ValueError("API mode does not support infill prompts")
            else:
                raise ValueError(f"Unsupported prompt keys: {prompt_contents.keys()}")
        else:
            raise ValueError(f"Unsupported prompt format: {type(prompt_contents)}")

        if is_chat:
            request_body = {
                "model": args.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": args.max_length_generation,
                "n": args.n_samples,
                "temperature": args.temperature if args.do_sample else 0.0,
                "top_p": args.top_p,
            }
        else:
            request_body = {
                "model": args.model,
                "prompt": prompt,
                "max_tokens": args.max_length_generation,
                "n": args.n_samples,
                "temperature": args.temperature if args.do_sample else 0.0,
                "top_p": args.top_p,
            }
        if api_stop:
            request_body["stop"] = api_stop

        try:
            response = session.post(api_endpoint, json=request_body, timeout=600)
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"\nAPI request failed for sample {sample_idx}: {e}")
            health.record_failure(e)
            # Append empty generations for this sample so indexing stays consistent
            generations.append(["" for _ in range(args.n_samples)])
            continue

        health.record_success()
        result = response.json()

        sample_generations = []
        for choice in result["choices"]:
            if is_chat:
                gen_text = choice.get("message", {}).get("content", "")
            else:
                gen_text = choice.get("text", "")
            # The API returns only the completion; prepend prompt to match local behavior
            # (postprocess_generation expects prompt + completion)
            full_text = prompt + gen_text
            if args.postprocess:
                full_text = task.postprocess_generation(full_text, dataset_idx)
            sample_generations.append(full_text)

        generations.append(sample_generations)

        # Write live event for streaming UI
        live_events_path = os.environ.get("BIGCODE_LIVE_EVENTS_PATH")
        if live_events_path:
            item = dataset[dataset_idx]
            task_id = item.get("task_id", f"task_{sample_idx}") if isinstance(item, dict) else f"task_{sample_idx}"
            # Strip the prompt prefix back out for the completion
            completions = []
            for gen in sample_generations:
                comp = gen[len(prompt):] if gen.startswith(prompt) else gen
                completions.append(comp)
            event = {
                "index": sample_idx,
                "total": n_tasks,
                "task_id": task_id,
                "prompt": prompt,
                "completions": completions,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                with open(live_events_path, "a") as ef:
                    ef.write(json.dumps(event) + "\n")
            except Exception as e:
                print(f"\nFailed to write live event: {e}")

        # Intermediate save
        if save_every_k_tasks >= 1 and (sample_idx + 1) % save_every_k_tasks == 0:
            if intermediate_save_generations_path:
                with open(intermediate_save_generations_path, "w") as fp:
                    json.dump(generations, fp)
                    print(f"\nintermediate generations saved at {intermediate_save_generations_path}")

    if health.all_failed:
        # Refuse to hand back a full set of empty strings. They would score as
        # failures and produce a plausible `pass@1: 0.0` for a run that never
        # reached the model — see AllGenerationsFailedError.
        reasons = ", ".join(f"{k}x{v}" for k, v in sorted(health.reasons.items()))
        raise AllGenerationsFailedError(
            f"All {health.attempted} generation requests to {api_endpoint} failed "
            f"({reasons}); refusing to score empty generations. This is an "
            f"infrastructure failure, not a model result."
        )

    if health.failed:
        # Partial failure is reported, not refused: the surviving generations
        # are still real model output, and the count is what tells you the score
        # is computed over fewer samples than it looks.
        print(
            f"\nWARNING: {health.failed}/{health.attempted} generation requests failed "
            f"({dict(health.reasons)}). The score below is computed with those "
            f"samples counted as failures."
        )

    return generations
