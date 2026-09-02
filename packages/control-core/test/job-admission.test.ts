import type { ActionIntent, CapacityProfileSpec } from "@harbor-hf/contracts";
import { describe, expect, it } from "vitest";
import { decideJobAdmission, type JobAdmissionState } from "../src/job-admission.js";
import { fairJobLaunchOrder, ordinaryActionOrder } from "../src/reconciler.js";

const capacity: CapacityProfileSpec = {
  namespace: "test",
  max_active_jobs: 4,
  hardware_limits: [{ hardware: "cpu-basic", max_active_jobs: 2 }],
  start_burst: 2,
  start_refill_tokens: 1,
  start_refill_period_seconds: 10,
};

const state: JobAdmissionState = {
  active_jobs: 0,
  active_hardware: 0,
  tokens: 2,
  refill_cursor_at: "2026-08-22T00:00:00.000Z",
};

function launch(runId: string, taskId: string, createdAt: string): ActionIntent {
  return {
    schema_version: "v1",
    kind: "action.intent",
    record_id: `action-${runId}-${taskId}`,
    created_at: createdAt,
    actor: { subject: "test", role: "service" },
    action_id: `action-${runId}-${taskId}`,
    run_id: runId,
    action_kind: "job.launch",
    generation: 0,
    target: taskId,
    payload: { task_id: taskId, task_ids: [taskId] },
  };
}

describe("Job admission", () => {
  it("round-robins runs while preserving FIFO inside each run", () => {
    const intents = [
      launch("run-a", "task-1", "2026-08-22T00:00:00.000Z"),
      launch("run-a", "task-2", "2026-08-22T00:00:01.000Z"),
      launch("run-b", "task-1", "2026-08-22T00:00:02.000Z"),
      launch("run-b", "task-2", "2026-08-22T00:00:03.000Z"),
    ];

    expect(
      fairJobLaunchOrder(intents, "run-a").map(
        (intent) => `${intent.run_id}:${intent.payload.task_id}`,
      ),
    ).toEqual(["run-b:task-1", "run-a:task-1", "run-b:task-2", "run-a:task-2"]);
  });

  it("prioritizes cancellation and admission over observation work", () => {
    const observed = {
      ...launch("run-a", "observed", "2026-08-22T00:00:00.000Z"),
      action_kind: "job.observe",
    } satisfies ActionIntent;
    const admitted = {
      ...launch("run-b", "admitted", "2026-08-22T00:00:01.000Z"),
      action_kind: "run.admit",
    } satisfies ActionIntent;
    const cancelled = {
      ...launch("run-c", "cancelled", "2026-08-22T00:00:02.000Z"),
      action_kind: "run.cancel",
    } satisfies ActionIntent;

    expect(
      ordinaryActionOrder([observed, admitted, cancelled]).map(
        (intent) => intent.action_kind,
      ),
    ).toEqual(["run.cancel", "run.admit", "job.observe"]);
  });

  it("admits one physical Job within every limit", () => {
    expect(
      decideJobAdmission(
        capacity,
        state,
        "cpu-basic",
        3,
        0,
        new Date("2026-08-22T00:00:00.000Z"),
      ),
    ).toEqual({
      status: "admitted",
      limiting_factor: null,
      not_before: null,
      tokens_remaining: 1,
      refill_cursor_at: "2026-08-22T00:00:00.000Z",
    });
  });

  it("clamps a lowered burst before the next refill", () => {
    expect(
      decideJobAdmission(
        { ...capacity, start_burst: 1 },
        { ...state, tokens: 2 },
        "cpu-basic",
        3,
        0,
        new Date("2026-08-22T00:00:09.000Z"),
      ),
    ).toMatchObject({
      status: "admitted",
      tokens_remaining: 0,
      refill_cursor_at: "2026-08-22T00:00:00.000Z",
    });
  });

  it.each([
    [{ active_jobs: 4 }, "namespace_job_capacity"],
    [{ active_hardware: 2 }, "hardware_job_capacity"],
    [{ tokens: 0 }, "start_rate"],
  ] as const)("defers when %s reaches its limit", (change, limitingFactor) => {
    expect(
      decideJobAdmission(
        capacity,
        { ...state, ...change },
        "cpu-basic",
        3,
        0,
        new Date("2026-08-22T00:00:00.000Z"),
      ).limiting_factor,
    ).toBe(limitingFactor);
  });

  it("distinguishes the per-Run Job limit", () => {
    expect(
      decideJobAdmission(
        capacity,
        state,
        "cpu-basic",
        3,
        3,
        new Date("2026-08-22T00:00:00.000Z"),
      ).limiting_factor,
    ).toBe("run_job_capacity");
  });
});
