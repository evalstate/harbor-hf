export interface Interval {
  low: number;
  high: number;
}

export interface ResultTaskSummary {
  task_id: string;
  outcome: string;
  reward: number | null;
  cost_microusd: number;
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface ResultAttemptInput {
  attempt_id: string;
  task_id: string;
  outcome: string;
  cost_microusd: number;
  metrics: Record<string, number>;
}

export interface ResultTaskInput {
  task_id: string;
  terminal_outcome: string | null;
  selected_attempt_id: string | null;
}

export interface ResultSummary {
  pass_count: number | null;
  pass_rate: number | null;
  pass_rate_ci95: Interval | null;
  input_tokens: number | null;
  output_tokens: number | null;
  inference_cost_microusd: number | null;
  mean_task_cost_microusd: number | null;
  task_cost_ci95: Interval | null;
  observed_cost_microusd: number | null;
  outputs_prefix: string | null;
  outputs_url: string | null;
  hf_uri: string | null;
  tasks: ResultTaskSummary[];
}

const Z95 = 1.959963984540054;

/**
 * Return a Wilson score 95% interval for a binomial proportion.
 *
 * `successes` is the number of passing trials. `n` is the locked task or
 * trial count. The interval is the usual operator-facing score CI for a
 * 0/1 Harbor reward.
 */
export function wilsonInterval(successes: number, n: number): Interval {
  if (!Number.isInteger(successes) || !Number.isInteger(n) || n <= 0 || successes < 0)
    throw new RangeError("Wilson interval requires non-negative integer counts");
  if (successes > n)
    throw new RangeError("Wilson interval cannot have more successes than trials");
  const p = successes / n;
  const z2 = Z95 * Z95;
  const denominator = 1 + z2 / n;
  const center = (p + z2 / (2 * n)) / denominator;
  const margin = (Z95 * Math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denominator;
  return {
    low: Math.max(0, center - margin),
    high: Math.min(1, center + margin),
  };
}

/**
 * Return a Wald 95% interval for the mean of per-task costs.
 *
 * One observation has no interval. The sample standard deviation uses `n - 1`.
 */
export function meanWaldInterval(values: readonly number[]): Interval | null {
  if (values.length < 2) return null;
  const n = values.length;
  const mean = values.reduce((sum, value) => sum + value, 0) / n;
  const variance =
    values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (n - 1);
  const margin = Z95 * Math.sqrt(variance / n);
  return { low: mean - margin, high: mean + margin };
}

export function outputsPrefix(
  resultPath: string | null | undefined,
  publicationId: string,
): string {
  if (resultPath?.includes("/"))
    return resultPath.slice(0, resultPath.lastIndexOf("/"));
  if (resultPath) return resultPath;
  return `results/schema=v1/publications/${publicationId}`;
}

export function bucketTreeUrl(bucketId: string, objectPath: string): string | null {
  const encoded = encodedBucketPath(bucketId, objectPath);
  return encoded
    ? `https://huggingface.co/buckets/${encoded.bucket}/tree/${encoded.path}`
    : null;
}

export function bucketHfUri(bucketId: string, objectPath: string): string | null {
  const encoded = encodedBucketPath(bucketId, objectPath);
  return encoded ? `hf://buckets/${bucketId}/${objectPath.replace(/^\/+/, "")}` : null;
}

function encodedBucketPath(
  bucketId: string,
  objectPath: string,
): { bucket: string; path: string } | null {
  const slash = bucketId.indexOf("/");
  if (
    slash <= 0 ||
    slash !== bucketId.lastIndexOf("/") ||
    slash === bucketId.length - 1
  )
    return null;
  const namespace = bucketId.slice(0, slash);
  const name = bucketId.slice(slash + 1);
  const path = objectPath
    .replace(/^\/+/, "")
    .split("/")
    .filter((segment) => segment.length > 0)
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  if (!namespace || !name) return null;
  return {
    bucket: `${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`,
    path,
  };
}

function metricNumber(metrics: Record<string, number>, key: string): number | null {
  if (!(key in metrics)) return null;
  const value = metrics[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Build the operator-facing result summary from a catalog row plus selected attempts.
 *
 * Pass rate is complete sealed tasks over the locked task count. Token and
 * inference cost come from selected attempt receipts. Bucket links are Hub
 * browse URLs for the published object prefix; the browser still has no Bucket
 * credential.
 */
export function summarizePublishedResult(input: {
  bucketId: string;
  publicationId: string;
  resultPath: string | null | undefined;
  catalogTaskCount: number | null | undefined;
  catalogStrictPassCount: number | null | undefined;
  observedCostMicrousd: number | null | undefined;
  tasks: readonly ResultTaskInput[];
  attempts: readonly ResultAttemptInput[];
}): ResultSummary {
  const prefix = outputsPrefix(input.resultPath, input.publicationId);
  const attemptsById = new Map(
    input.attempts.map((attempt) => [attempt.attempt_id, attempt]),
  );
  const selected = input.tasks.flatMap((task) => {
    if (!task.selected_attempt_id) return [];
    const attempt = attemptsById.get(task.selected_attempt_id);
    return attempt ? [attempt] : [];
  });
  const taskSummaries: ResultTaskSummary[] = selected.map((attempt) => ({
    task_id: attempt.task_id,
    outcome: attempt.outcome,
    reward: metricNumber(attempt.metrics, "reward"),
    cost_microusd: attempt.cost_microusd,
    input_tokens: metricNumber(attempt.metrics, "input_tokens"),
    output_tokens: metricNumber(attempt.metrics, "output_tokens"),
  }));
  const taskCount =
    typeof input.catalogTaskCount === "number" && input.catalogTaskCount > 0
      ? input.catalogTaskCount
      : input.tasks.length > 0
        ? input.tasks.length
        : null;
  const completeCount = input.tasks.filter(
    (task) => task.terminal_outcome === "complete",
  ).length;
  const passCount =
    input.tasks.length > 0
      ? completeCount
      : typeof input.catalogStrictPassCount === "number"
        ? input.catalogStrictPassCount
        : null;
  const passRate =
    passCount === null || taskCount === null || taskCount === 0
      ? null
      : passCount / taskCount;
  const costs = taskSummaries.map((task) => task.cost_microusd);
  const inferenceCost = costs.length
    ? costs.reduce((sum, value) => sum + value, 0)
    : null;
  const inputTokens = taskSummaries.reduce<number | null>((sum, task) => {
    if (task.input_tokens === null) return sum;
    return (sum ?? 0) + task.input_tokens;
  }, null);
  const outputTokens = taskSummaries.reduce<number | null>((sum, task) => {
    if (task.output_tokens === null) return sum;
    return (sum ?? 0) + task.output_tokens;
  }, null);
  return {
    pass_count: passCount,
    pass_rate: passRate,
    pass_rate_ci95:
      passCount === null || taskCount === null
        ? null
        : wilsonInterval(passCount, taskCount),
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    inference_cost_microusd: inferenceCost,
    mean_task_cost_microusd:
      inferenceCost === null ? null : inferenceCost / costs.length,
    task_cost_ci95: meanWaldInterval(costs),
    observed_cost_microusd:
      typeof input.observedCostMicrousd === "number"
        ? input.observedCostMicrousd
        : null,
    outputs_prefix: prefix,
    outputs_url: bucketTreeUrl(input.bucketId, prefix),
    hf_uri: bucketHfUri(input.bucketId, prefix),
    tasks: taskSummaries,
  };
}
