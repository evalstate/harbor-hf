"""Temporary Harbor 0.21.0 patch for empty progress metrics.

DELETE THIS MODULE when the pinned Harbor version includes
https://github.com/harbor-framework/harbor/pull/2681 (the first stable release
after 0.21.0). Harbor 0.21.0 was still the latest PyPI release on 2026-08-21.

Harbor 0.21.0 seeds live progress metrics only for ``adhoc`` and configured
datasets. A direct locked task can still carry a dataset ``source`` after Harbor
reloads the task, even when the worker omits that label from JobConfig. The
trial finishes and writes ``result.json``, then ``Job._update_metric_display``
indexes an empty metric list and raises ``IndexError``.

The execution worker cannot monkeypatch the ``harbor run`` subprocess in-process.
It drops a ``sitecustomize.py`` on ``PYTHONPATH`` so the Harbor CLI applies the
upstream fix: seed task sources in ``_resolve_metrics``, and skip progress
display when the metric list is empty.
"""

from __future__ import annotations

import os
from importlib.metadata import version
from pathlib import Path
from typing import Any

from harbor.metrics.base import BaseMetric
from harbor.models.job.config import JobConfig
from harbor.models.trial.config import TaskConfig
from harbor.trial.hooks import TrialHookEvent

# Delete this constant with the module when Harbor ships PR 2681.
AFFECTED_HARBOR_VERSION = "0.21.0"
_PATCH_FLAG = "_hhf_0210_empty_metrics_patched"
_SITECUSTOMIZE = """\
from harbor_hf_agents.support.harbor_0210_empty_metrics import (
    apply_harbor_0210_empty_metrics_patch,
)

apply_harbor_0210_empty_metrics_patch()
"""

type _MetricsMap = dict[str, list[BaseMetric[Any]]]  # noqa: ANN401


def seed_task_source_metrics(
    metrics: _MetricsMap, task_configs: list[TaskConfig]
) -> None:
    """Copy adhoc job metrics onto any direct task source Harbor did not seed."""
    job_metrics = list(metrics["adhoc"])
    for task_config in task_configs:
        source = task_config.source or "adhoc"
        if source not in metrics:
            metrics[source] = list(job_metrics)


def metric_display_ready(metrics: _MetricsMap | None, dataset_name: str) -> bool:
    """Return whether progress display can index the first metric for this source."""
    return bool(metrics and dataset_name in metrics and metrics[dataset_name])


def apply_harbor_0210_empty_metrics_patch() -> None:
    """Patch Harbor 0.21.0 Job metric seeding and progress display in this process."""
    if version("harbor") != AFFECTED_HARBOR_VERSION:
        return
    from harbor.job import Job

    if getattr(Job, _PATCH_FLAG, False):
        return
    original_resolve = Job._resolve_metrics
    original_display = Job._update_metric_display

    async def wrapped_resolve(
        config: JobConfig, task_configs: list[TaskConfig]
    ) -> _MetricsMap:
        metrics = await original_resolve(config, task_configs)
        seed_task_source_metrics(metrics, task_configs)
        return metrics

    def wrapped_display(
        self: Job,
        event: TrialHookEvent,
        loading_progress: object,
        loading_progress_task: object,
    ) -> None:
        dataset_name = event.config.task.source or "adhoc"
        if not metric_display_ready(self._metrics, dataset_name):
            return
        original_display(self, event, loading_progress, loading_progress_task)

    setattr(Job, "_resolve_metrics", staticmethod(wrapped_resolve))  # noqa: B010
    setattr(Job, "_update_metric_display", wrapped_display)  # noqa: B010
    setattr(Job, _PATCH_FLAG, True)


def install_sitecustomize(root: Path) -> Path:
    """Write the Harbor CLI sitecustomize shim under ``root``."""
    directory = root / "harbor-0210-empty-metrics"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    return directory


def harbor_cli_env(root: Path) -> dict[str, str]:
    """Environment for ``harbor run`` with the empty-metrics sitecustomize first."""
    patch_dir = install_sitecustomize(root)
    env = os.environ.copy()
    pythonpath = str(patch_dir)
    if "PYTHONPATH" in env and env["PYTHONPATH"]:
        pythonpath = f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath
    return env
