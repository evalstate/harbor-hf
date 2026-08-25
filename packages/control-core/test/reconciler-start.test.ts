import { NoopActions } from "@harbor-hf/hf-adapters";
import { createTestControl } from "@harbor-hf/test-fixtures";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResultPublisher } from "../src/publication.js";
import { Reconciler } from "../src/reconciler.js";

const controls: Awaited<ReturnType<typeof createTestControl>>[] = [];

afterEach(async () => {
  vi.useRealTimers();
  await Promise.all(controls.splice(0).map((control) => control.close()));
});

describe("Reconciler start", () => {
  it("reports a rejected tick and keeps the timer stoppable", async () => {
    const control = await createTestControl();
    controls.push(control);
    const reconciler = new Reconciler(
      control.service,
      control.projection,
      new NoopActions(),
      new ResultPublisher(control.store, control.projection, control.service),
      { interval_ms: 1_000, observation_interval_ms: 0, batch_size: 16 },
    );
    const failure = new Error("expected reconciliation failure");
    vi.spyOn(reconciler, "tick").mockRejectedValueOnce(failure).mockResolvedValue(0);
    const onError = vi.fn();

    reconciler.start(undefined, onError);
    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith(failure));
    await reconciler.stop();
  });

  it("starts the sync cadence after initialization", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-24T00:00:00.000Z"));
    const control = await createTestControl();
    controls.push(control);
    await control.service.submit(
      {
        benchmark: "control-smoke",
        model: "control-smoke",
        harness: "control-smoke",
        deployment: "hf-cpu-smoke",
        launch_policy: "control-smoke",
        ceiling_microusd: 0,
        confirmed: true,
      },
      "post-initialization-sync-cadence",
      { subject: "operator", role: "operator" },
    );
    const reconciler = new Reconciler(
      control.service,
      control.projection,
      new NoopActions(),
      new ResultPublisher(control.store, control.projection, control.service),
      {
        interval_ms: 2_000,
        sync_interval_ms: 30_000,
        observation_interval_ms: 0,
        batch_size: 16,
      },
    );
    const sync = vi.spyOn(control.service, "syncProjection");
    vi.setSystemTime(new Date("2026-08-24T00:05:00.000Z"));

    reconciler.start();
    await reconciler.stop();

    expect(sync).not.toHaveBeenCalled();
  });

  it("keeps Bucket sync on its own deterministic cadence", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-24T00:00:00.000Z"));
    const control = await createTestControl();
    controls.push(control);
    const run = await control.service.submit(
      {
        benchmark: "control-smoke",
        model: "control-smoke",
        harness: "control-smoke",
        deployment: "hf-cpu-smoke",
        launch_policy: "control-smoke",
        ceiling_microusd: 0,
        confirmed: true,
      },
      "sync-cadence-run",
      { subject: "operator", role: "operator" },
    );
    const activeRuns = await control.projection.activeRuns();
    const activeRun = activeRuns[0];
    if (!activeRun) throw new Error("expected an active test run");
    vi.spyOn(control.projection, "activeRuns").mockResolvedValue([
      activeRun,
      { ...activeRun, run_id: "run-secondary" },
    ]);
    const reconciler = new Reconciler(
      control.service,
      control.projection,
      new NoopActions(),
      new ResultPublisher(control.store, control.projection, control.service),
      {
        interval_ms: 2_000,
        sync_interval_ms: 30_000,
        observation_interval_ms: 0,
        batch_size: 16,
      },
    );
    const sync = vi.spyOn(control.service, "syncProjection").mockResolvedValue(0);

    await reconciler.tick();
    vi.setSystemTime(new Date("2026-08-24T00:00:29.999Z"));
    await reconciler.tick();
    expect(sync).not.toHaveBeenCalled();

    vi.setSystemTime(new Date("2026-08-24T00:00:30.000Z"));
    await reconciler.tick();
    expect(sync).toHaveBeenCalledTimes(1);
    expect(sync).toHaveBeenLastCalledWith(`control/schema=v1/runs/${run.run_id}/tasks`);

    vi.setSystemTime(new Date("2026-08-24T00:00:59.999Z"));
    await reconciler.tick();
    expect(sync).toHaveBeenCalledTimes(1);
    vi.setSystemTime(new Date("2026-08-24T00:01:00.000Z"));
    await reconciler.tick();
    expect(sync).toHaveBeenCalledTimes(2);
    expect(sync).toHaveBeenLastCalledWith("control/schema=v1/runs/run-secondary/tasks");
  });

  it("does not rescan every historical record while idle", async () => {
    const control = await createTestControl();
    controls.push(control);
    const reconciler = new Reconciler(
      control.service,
      control.projection,
      new NoopActions(),
      new ResultPublisher(control.store, control.projection, control.service),
      {
        interval_ms: 2_000,
        sync_interval_ms: 0,
        observation_interval_ms: 0,
        batch_size: 16,
      },
    );
    const sync = vi.spyOn(control.service, "syncProjection");

    await reconciler.tick();

    expect(sync).not.toHaveBeenCalled();
  });
});
