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

  it("keeps Bucket sync on its own deterministic cadence", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-24T00:00:00.000Z"));
    const control = await createTestControl();
    controls.push(control);
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

    vi.setSystemTime(new Date("2026-08-24T00:00:59.999Z"));
    await reconciler.tick();
    expect(sync).toHaveBeenCalledTimes(1);
    vi.setSystemTime(new Date("2026-08-24T00:01:00.000Z"));
    await reconciler.tick();
    expect(sync).toHaveBeenCalledTimes(2);
  });
});
