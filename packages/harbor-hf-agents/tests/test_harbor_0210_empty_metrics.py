from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from types import SimpleNamespace

import pytest

from harbor_hf_agents.support.harbor_0210_empty_metrics import (
    AFFECTED_HARBOR_VERSION,
    apply_harbor_0210_empty_metrics_patch,
    harbor_cli_env,
    install_sitecustomize,
    metric_display_ready,
    seed_task_source_metrics,
)


def test_seeds_direct_task_sources_from_adhoc_metrics() -> None:
    metrics: dict[str, list[object]] = {"adhoc": ["mean"]}

    seed_task_source_metrics(metrics, [SimpleNamespace(source="example-dataset")])

    assert metrics["example-dataset"] == ["mean"]


def test_leaves_adhoc_and_existing_sources_alone() -> None:
    metrics: dict[str, list[object]] = {"adhoc": ["mean"], "kept": ["other"]}

    seed_task_source_metrics(
        metrics,
        [SimpleNamespace(source=None), SimpleNamespace(source="kept")],
    )

    assert metrics == {"adhoc": ["mean"], "kept": ["other"]}


def test_skips_progress_display_when_the_metric_list_is_empty() -> None:
    assert metric_display_ready(None, "adhoc") is False
    assert metric_display_ready({}, "adhoc") is False
    assert metric_display_ready({"adhoc": []}, "adhoc") is False
    assert metric_display_ready({"adhoc": ["mean"]}, "other") is False
    assert metric_display_ready({"adhoc": ["mean"]}, "adhoc") is True


def test_installs_sitecustomize_that_applies_the_patch(tmp_path) -> None:
    directory = install_sitecustomize(tmp_path)

    text = (directory / "sitecustomize.py").read_text()
    assert "DELETE THIS MODULE" not in text
    assert "apply_harbor_0210_empty_metrics_patch" in text
    env = harbor_cli_env(tmp_path)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(directory)


def test_apply_is_a_noop_when_harbor_is_not_0_21_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harbor.job import Job

    original_resolve = Job._resolve_metrics
    monkeypatch.setattr(
        "harbor_hf_agents.support.harbor_0210_empty_metrics.version",
        lambda _name: "0.22.0",
    )

    apply_harbor_0210_empty_metrics_patch()

    assert Job._resolve_metrics is original_resolve
    assert getattr(Job, "_hhf_0210_empty_metrics_patched", False) is False


def test_apply_wraps_resolve_and_skips_empty_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harbor.job import Job

    original_resolve = Job._resolve_metrics
    original_display = Job._update_metric_display
    patched = getattr(Job, "_hhf_0210_empty_metrics_patched", False)
    monkeypatch.setattr(
        "harbor_hf_agents.support.harbor_0210_empty_metrics.version",
        lambda _name: AFFECTED_HARBOR_VERSION,
    )

    async def fake_resolve(_config, _task_configs):
        return defaultdict(list, {"adhoc": ["mean"]})

    def fake_display(self, event, _progress, _task) -> None:
        dataset_name = event.config.task.source or "adhoc"
        self._metrics[dataset_name][0]

    Job._resolve_metrics = staticmethod(fake_resolve)
    Job._update_metric_display = fake_display
    Job._hhf_0210_empty_metrics_patched = False
    try:
        apply_harbor_0210_empty_metrics_patch()
        apply_harbor_0210_empty_metrics_patch()

        async def _run() -> dict[str, list[object]]:
            return await Job._resolve_metrics(
                None, [SimpleNamespace(source="example-dataset")]
            )

        metrics = asyncio.run(_run())
        holder = SimpleNamespace(_metrics={"example-dataset": []})
        event = SimpleNamespace(
            config=SimpleNamespace(task=SimpleNamespace(source="example-dataset"))
        )
        Job._update_metric_display(holder, event, None, None)
    finally:
        Job._resolve_metrics = original_resolve
        Job._update_metric_display = original_display
        Job._hhf_0210_empty_metrics_patched = patched

    assert metrics["example-dataset"] == ["mean"]
