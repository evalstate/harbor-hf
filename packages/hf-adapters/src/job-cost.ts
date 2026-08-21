const terminalStates = new Set([
  "COMPLETED",
  "STOPPED",
  "ERROR",
  "DELETED",
  "CANCELED",
  "CANCELLED",
]);

export interface JobTimeBounds {
  startedAt?: string | null;
  finishedAt?: string | null;
  updatedAt?: string | null;
  status?: { stage?: string } | null;
}

/**
 * Accrues locked Job hardware cost from wall-clock hours.
 * Unstarted Jobs cost nothing. A running Job uses elapsed time through now.
 */
export function jobHardwareCostMicrousd(
  job: JobTimeBounds,
  hourlyMicrousd: number,
  nowMs = Date.now(),
): number {
  if (!Number.isInteger(hourlyMicrousd) || hourlyMicrousd < 0)
    throw new Error("Job hourly cost is invalid");
  if (hourlyMicrousd === 0) return 0;
  const started = Date.parse(job.startedAt ?? "");
  if (!Number.isFinite(started)) return 0;
  const stage = (job.status?.stage ?? "").toUpperCase();
  const finishedRaw =
    job.finishedAt ?? (terminalStates.has(stage) ? job.updatedAt : null);
  const finished = finishedRaw ? Date.parse(finishedRaw) : nowMs;
  if (!Number.isFinite(finished) || finished < started)
    throw new Error("Job timestamps are invalid");
  return Math.ceil(((finished - started) / 1000 / 3600) * hourlyMicrousd);
}
