"""Tests for code_eval.cgroup_utils' container-CPU-aware thread pool sizing.

Covers self.code-eval issue #3: ThreadPoolExecutor in
code_eval/tasks/multiple.py used to size off os.cpu_count() (the host's real
CPU count) rather than the container's cgroup CPU limit, risking far more
concurrent sandboxed subprocess evaluations than the container's memory
budget could hold under n_samples > 1.
"""

import pytest

from code_eval import cgroup_utils


@pytest.fixture(autouse=True)
def _isolate_cgroup_paths(tmp_path, monkeypatch):
    """Point the module's cgroup path constants at paths that don't exist by
    default, so tests never accidentally read the real host/CI cgroup files.
    Each test opts in to whichever path(s) it wants present.
    """
    monkeypatch.setattr(cgroup_utils, "_CGROUP_V2_CPU_MAX", str(tmp_path / "cpu.max"))
    monkeypatch.setattr(cgroup_utils, "_CGROUP_V1_CPU_QUOTA", str(tmp_path / "cfs_quota_us"))
    monkeypatch.setattr(cgroup_utils, "_CGROUP_V1_CPU_PERIOD", str(tmp_path / "cfs_period_us"))
    return tmp_path


class TestCgroupV2:
    def test_one_core_limit(self, tmp_path):
        # Matches the real self-code-eval pod: cpu.max == "100000 100000".
        (tmp_path / "cpu.max").write_text("100000 100000\n")
        assert cgroup_utils.get_container_cpu_limit() == pytest.approx(1.0)
        # floor(1.0) - 1 = 0, clamped up to the minimum of 1 worker.
        assert cgroup_utils.get_max_workers() == 1

    def test_four_core_limit(self, tmp_path):
        (tmp_path / "cpu.max").write_text("400000 100000\n")
        assert cgroup_utils.get_container_cpu_limit() == pytest.approx(4.0)
        assert cgroup_utils.get_max_workers() == 3

    def test_fractional_limit_floors_before_headroom(self, tmp_path):
        (tmp_path / "cpu.max").write_text("150000 100000\n")
        assert cgroup_utils.get_container_cpu_limit() == pytest.approx(1.5)
        # floor(1.5) - 1 = 0, clamped up to 1.
        assert cgroup_utils.get_max_workers() == 1

    def test_unlimited_max_falls_back_to_host_cpu_count(self, tmp_path, monkeypatch):
        (tmp_path / "cpu.max").write_text("max 100000\n")
        assert cgroup_utils.get_container_cpu_limit() is None
        monkeypatch.setattr(cgroup_utils, "cpu_count", lambda: 8)
        assert cgroup_utils.get_max_workers() == 7


class TestCgroupV1:
    def test_two_core_limit(self, tmp_path):
        (tmp_path / "cfs_quota_us").write_text("200000\n")
        (tmp_path / "cfs_period_us").write_text("100000\n")
        assert cgroup_utils.get_container_cpu_limit() == pytest.approx(2.0)
        assert cgroup_utils.get_max_workers() == 1

    def test_unlimited_quota_falls_back_to_host_cpu_count(self, tmp_path, monkeypatch):
        (tmp_path / "cfs_quota_us").write_text("-1\n")
        (tmp_path / "cfs_period_us").write_text("100000\n")
        assert cgroup_utils.get_container_cpu_limit() is None
        monkeypatch.setattr(cgroup_utils, "cpu_count", lambda: 4)
        assert cgroup_utils.get_max_workers() == 3

    def test_v2_present_takes_priority_over_v1(self, tmp_path):
        (tmp_path / "cpu.max").write_text("300000 100000\n")
        (tmp_path / "cfs_quota_us").write_text("100000\n")
        (tmp_path / "cfs_period_us").write_text("100000\n")
        # If v1 (1 core) were used instead of v2 (3 cores) this would be 1.
        assert cgroup_utils.get_max_workers() == 2


class TestNoCgroupFallback:
    def test_no_cgroup_files_falls_back_to_host_cpu_count(self, monkeypatch):
        # Neither cpu.max nor the v1 quota/period files exist at the
        # redirected tmp_path locations set up by the autouse fixture.
        assert cgroup_utils.get_container_cpu_limit() is None
        monkeypatch.setattr(cgroup_utils, "cpu_count", lambda: 32)
        assert cgroup_utils.get_max_workers() == 31

    def test_single_host_cpu_never_zeroes_out(self, monkeypatch):
        assert cgroup_utils.get_container_cpu_limit() is None
        monkeypatch.setattr(cgroup_utils, "cpu_count", lambda: 1)
        assert cgroup_utils.get_max_workers() == 1
