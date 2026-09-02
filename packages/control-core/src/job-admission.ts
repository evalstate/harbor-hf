import type { CapacityProfileSpec } from "@harbor-hf/contracts";

export type JobLimitingFactor =
  | "run_job_capacity"
  | "namespace_job_capacity"
  | "hardware_job_capacity"
  | "start_rate";

export interface JobAdmissionState {
  active_jobs: number;
  active_hardware: number;
  tokens: number;
  refill_cursor_at: string;
}

export interface JobAdmissionDecision {
  status: "admitted" | "deferred";
  limiting_factor: JobLimitingFactor | null;
  not_before: string | null;
  tokens_remaining: number;
  refill_cursor_at: string;
}

function refill(
  policy: CapacityProfileSpec,
  state: JobAdmissionState,
  now: Date,
): { tokens: number; cursor: Date } {
  const cursor = new Date(state.refill_cursor_at);
  if (!Number.isFinite(cursor.getTime())) throw new Error("refill cursor is invalid");
  const periodMs = policy.start_refill_period_seconds * 1000;
  const elapsed = Math.max(0, now.getTime() - cursor.getTime());
  const periods = Math.floor(elapsed / periodMs);
  // A promoted policy can lower the burst below the previous grant's state.
  // Clamp before refill so the next immutable grant always fits its profile.
  const tokens = Math.min(state.tokens, policy.start_burst);
  if (periods === 0) return { tokens, cursor };
  return {
    tokens: Math.min(policy.start_burst, tokens + periods * policy.start_refill_tokens),
    cursor: new Date(cursor.getTime() + periods * periodMs),
  };
}

export function decideJobAdmission(
  policy: CapacityProfileSpec,
  state: JobAdmissionState,
  hardware: string,
  runMaxJobs: number,
  runActiveJobs: number,
  now: Date,
): JobAdmissionDecision {
  if (!Number.isInteger(runMaxJobs) || runMaxJobs < 1 || runMaxJobs > 1024)
    throw new Error("run Job cap must be an integer from 1 to 1024");

  const refillState = refill(policy, state, now);
  const hardwareLimit = policy.hardware_limits.find(
    (limit) => limit.hardware === hardware,
  );
  if (state.active_jobs >= policy.max_active_jobs)
    return {
      status: "deferred",
      limiting_factor: "namespace_job_capacity",
      not_before: null,
      tokens_remaining: refillState.tokens,
      refill_cursor_at: refillState.cursor.toISOString(),
    };
  if (runActiveJobs >= runMaxJobs)
    return {
      status: "deferred",
      limiting_factor: "run_job_capacity",
      not_before: null,
      tokens_remaining: refillState.tokens,
      refill_cursor_at: refillState.cursor.toISOString(),
    };
  if (hardwareLimit && state.active_hardware >= hardwareLimit.max_active_jobs)
    return {
      status: "deferred",
      limiting_factor: "hardware_job_capacity",
      not_before: null,
      tokens_remaining: refillState.tokens,
      refill_cursor_at: refillState.cursor.toISOString(),
    };
  if (refillState.tokens === 0)
    return {
      status: "deferred",
      limiting_factor: "start_rate",
      not_before: new Date(
        refillState.cursor.getTime() + policy.start_refill_period_seconds * 1000,
      ).toISOString(),
      tokens_remaining: 0,
      refill_cursor_at: refillState.cursor.toISOString(),
    };
  return {
    status: "admitted",
    limiting_factor: null,
    not_before: null,
    tokens_remaining: refillState.tokens - 1,
    refill_cursor_at: refillState.cursor.toISOString(),
  };
}
