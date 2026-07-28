"""Container-CPU-aware thread pool sizing.

Split out as a standalone, dependency-light module (stdlib only) so it can be
imported and unit-tested without pulling in the rest of code_eval.tasks'
heavy ML stack (torch, transformers, etc via code_eval/tasks/__init__.py).

Background: ThreadPoolExecutor pools in this codebase (e.g.
code_eval/tasks/multiple.py's evaluate_problem call) used to size off
os.cpu_count() / multiprocessing.cpu_count(), which reads the HOST's real CPU
count. Under Kubernetes that can differ wildly from the container's cgroup CPU
limit (e.g. 32 host cores vs. a 1-core pod limit) -- sizing off the host count
risks spinning up far more concurrent sandboxed subprocess evaluations than
the container's memory budget can hold, especially under n_samples > 1. See
self.code-eval issue #3.

No separate memory-aware cap (deliberate, not an oversight): the self-code-eval
pod's real limits are 1 CPU core / 2Gi memory. get_max_workers() on that pod
returns 1 (see module tests), so the sandboxed-subprocess pool can never run
more than one evaluation process concurrently regardless of n_samples --
worst case is a single process at a time. The heaviest measured per-process
RSS across languages (Racket, ~123MB) is >16x under the 2Gi limit even in
that single-worker worst case, so CPU-based sizing alone already leaves a
large safety margin here; a memory-aware cap would currently be a no-op. If
the pod's CPU limit is raised substantially in the future without a
proportional memory bump, revisit this -- e.g. a pod resized to 16 CPUs on
the same 2Gi would compute 15 workers, and 15 * 123MB (~1.8GB) would erode
most of that margin.
"""

import math
from multiprocessing import cpu_count
from typing import Optional

# cgroup CPU-limit files, checked in this order (v2 first -- it's what current
# kernels/container runtimes use; v1 as a fallback for older hosts).
_CGROUP_V2_CPU_MAX = "/sys/fs/cgroup/cpu.max"
_CGROUP_V1_CPU_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CGROUP_V1_CPU_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"


def _read_cgroup_v2_cpu_limit() -> Optional[float]:
    """Read the CPU limit (in whole-core units) from cgroup v2's cpu.max.

    Format is "$MAX $PERIOD" in microseconds, e.g. "100000 100000" for a
    1-core limit. MAX may be the literal string "max", meaning unlimited --
    treated as "no limit set" (returns None) so the caller falls back.
    """
    try:
        with open(_CGROUP_V2_CPU_MAX) as f:
            max_str, period_str = f.read().split()
    except (FileNotFoundError, ValueError, OSError):
        return None
    if max_str == "max":
        return None
    try:
        quota, period = int(max_str), int(period_str)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def _read_cgroup_v1_cpu_limit() -> Optional[float]:
    """Read the CPU limit (in whole-core units) from cgroup v1's
    cpu.cfs_quota_us / cpu.cfs_period_us. A quota of -1 means unlimited.
    """
    try:
        with open(_CGROUP_V1_CPU_QUOTA) as f:
            quota = int(f.read().strip())
        with open(_CGROUP_V1_CPU_PERIOD) as f:
            period = int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def get_container_cpu_limit() -> Optional[float]:
    """Return the container's CPU limit in whole-core units (e.g. 1.0 for a
    1-core limit, 0.5 for a 500m limit).

    Checks cgroup v2 first, then cgroup v1. Returns None if neither is
    readable or reports an explicit limit (e.g. running outside a container
    during local dev, or a cgroup that reports "unlimited") -- callers should
    fall back to os.cpu_count() in that case.
    """
    limit = _read_cgroup_v2_cpu_limit()
    if limit is not None:
        return limit
    return _read_cgroup_v1_cpu_limit()


def get_max_workers() -> int:
    """Size a sandboxed-execution thread pool off the container's actual CPU
    allotment, not the host's physical core count.

    Mirrors the original "leave one core of headroom" intent of
    cpu_count() - 1, scaled to the container's real cgroup CPU limit, floored
    to a whole number of workers, and always at least 1. Falls back to
    cpu_count()-based sizing when no cgroup CPU limit is readable (local dev,
    non-containerized runs).
    """
    cpu_limit = get_container_cpu_limit()
    if cpu_limit is not None and cpu_limit > 0:
        return max(1, math.floor(cpu_limit) - 1)
    host_cpus = cpu_count()
    return host_cpus - 1 if host_cpus > 1 else 1
