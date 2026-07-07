import json
import os
import warnings
from datetime import datetime, timezone
from typing import List, Optional

import requests
from tqdm import tqdm


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
):
    """Generate completions via OpenAI-compatible API (chat or text completions)."""
    if args.load_generations_path:
        with open(args.load_generations_path) as fp:
            generations = json.load(fp)
            print(
                f"generations loaded, {n_tasks} selected from {len(generations)} with {len(generations[0])} candidates"
            )
        return generations[:n_tasks]

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
            # Append empty generations for this sample so indexing stays consistent
            generations.append(["" for _ in range(args.n_samples)])
            continue

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

    return generations
