import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function formatMoney(microusd: number): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: microusd < 1_000_000 ? 4 : 2,
  }).format(microusd / 1_000_000);
}

export function estimateLaunchReservationMicrousd(
  taskCount: number,
  deployment: Record<string, unknown> | undefined,
  policy: Record<string, unknown> | undefined,
): number {
  const reservation = Number(policy?.reservation_microusd ?? 0);
  const tasksPerJob = Number(deployment?.worker_max_tasks_per_job ?? 0);
  const executionJobs =
    taskCount === 0
      ? 0
      : Number.isSafeInteger(tasksPerJob) && tasksPerJob > 0
        ? Math.ceil(taskCount / tasksPerJob)
        : 1;
  const preparationAttempts =
    deployment?.preparation === "required"
      ? Number(policy?.max_preparation_attempts ?? 1)
      : 0;
  const preparationReservation = Number(policy?.preparation_reservation_microusd ?? 0);
  return executionJobs * reservation + preparationAttempts * preparationReservation;
}

export function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export function formatPercentInterval(interval: { low: number; high: number }): string {
  return `${formatPercent(interval.low)}–${formatPercent(interval.high)}`;
}

export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
}

/** Digests and Job IDs stay compact. Run names stay complete. */
export function shortId(value: string): string {
  return value.length > 24 ? `${value.slice(0, 14)}…${value.slice(-7)}` : value;
}

export const runNameClass = "block min-w-0 break-all font-mono text-xs";

export function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .replaceAll(".", " ")
    .replace(/^./, (letter) => letter.toUpperCase());
}
