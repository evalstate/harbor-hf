import { humanize } from "./lib";

export const LAUNCH_DEFAULTS = {
  benchmark: "terminal-bench-2-1-diagnostic-1",
  model: "gpt-oss-20b",
  harnessAgent: "opencode",
  reasoning: "off",
  deploymentKind: "providers",
} as const;

export const REASONING_OPTIONS = [
  ["off", "None"],
  ["minimal", "Minimal"],
  ["low", "Low"],
  ["medium", "Medium"],
  ["high", "High"],
  ["xhigh", "Extra high"],
] as const;

export const BENCHMARK_LAUNCH_POLICY: Record<string, string> = {
  "control-smoke": "control-smoke",
  "terminal-bench-2-1-canary": "tb21-canary",
  "terminal-bench-2-1-diagnostic-1": "tb21-diagnostic-1",
  "terminal-bench-2-1-official-5": "tb21-official-5",
  "terminal-bench-2-1-replacement": "tb21-replacement",
};

export type DeploymentKind = "providers" | "endpoints";

export function preferredAlias(
  preferred: string,
  available: readonly string[],
): string {
  if (!available.includes(preferred))
    throw new Error(`approved default ${preferred} is missing`);
  return preferred;
}

export function launchPolicyForBenchmark(benchmarkAlias: string): string {
  const policy = BENCHMARK_LAUNCH_POLICY[benchmarkAlias];
  if (!policy) throw new Error(`no launch policy is configured for ${benchmarkAlias}`);
  return policy;
}

export function doubleReservationMicrousd(estimatedMicrousd: number): number {
  return estimatedMicrousd * 2;
}

export function counted(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

export function deploymentKind(
  spec: Record<string, unknown>,
): DeploymentKind | "other" {
  if (typeof spec.inference_provider === "string" && spec.inference_provider.length > 0)
    return "providers";
  const template = spec.sandbox_template;
  if (!template || typeof template !== "object") return "other";
  const upstream = (template as Record<string, unknown>).inference_upstream;
  if (typeof upstream !== "string" || upstream.length === 0) return "other";
  if (upstream.includes("router.huggingface.co")) return "providers";
  if (upstream === "<redacted>") return "other";
  return "endpoints";
}

export function harnessAgent(spec: Record<string, unknown>): string {
  const agent = spec.agent;
  if (typeof agent !== "string" || agent.length === 0)
    throw new Error("harness profile is missing agent");
  return agent;
}

export function harnessReasoning(spec: Record<string, unknown>): string {
  const value = spec.reasoning_effort;
  return typeof value === "string" ? value : "off";
}

export function profileLabel(
  kind: string,
  alias: string,
  spec: Record<string, unknown>,
): string {
  if (kind === "benchmark") {
    const benchmark = typeof spec.benchmark === "string" ? spec.benchmark : alias;
    const sources = Array.isArray(spec.source_task_ids)
      ? new Set(spec.source_task_ids).size
      : 0;
    const tasks =
      sources > 0 ? sources : Array.isArray(spec.task_ids) ? spec.task_ids.length : 0;
    const trials = Array.isArray(spec.trial_indices)
      ? new Set(spec.trial_indices).size
      : 0;
    const name =
      benchmark === "terminal-bench-2-1" ? "Terminal-Bench 2.1" : humanize(benchmark);
    if (tasks === 0) return name;
    if (trials > 1)
      return `${name} · ${counted(tasks, "task")} with ${counted(trials, "trial")} each`;
    return `${name} · ${counted(tasks, "task")}`;
  }
  if (kind === "model")
    return typeof spec.model_id === "string" ? spec.model_id : alias;
  if (kind === "harness") {
    const agent = typeof spec.agent === "string" ? spec.agent : alias;
    if (agent === "dsh" || agent.startsWith("dsh-") || alias.startsWith("dsh"))
      return "DeepSeek Harness";
    if (agent === "opencode") return "OpenCode";
    if (agent === "pi") return "Pi";
    if (agent === "control-smoke") return "Control smoke";
    return humanize(agent);
  }
  return alias;
}

/** Operator-facing harness name. Profile aliases such as `dsh` stay as data. */
export function labeledHarness(value: string | null | undefined): string {
  if (!value) return "—";
  return profileLabel("harness", value, { agent: value });
}

export function selectHarnessAlias(
  harnesses: ReadonlyArray<{ alias: string; spec: Record<string, unknown> }>,
  agent: string,
  reasoning: string,
): string {
  const match = harnesses.find(
    (item) =>
      harnessAgent(item.spec) === agent && harnessReasoning(item.spec) === reasoning,
  );
  if (!match)
    throw new Error(
      `no approved ${agent} harness with reasoning ${reasoning} is available`,
    );
  return match.alias;
}

export function selectDeploymentAlias(
  deployments: ReadonlyArray<{ alias: string; spec: Record<string, unknown> }>,
  kind: DeploymentKind,
  model: string,
  harness: string,
): string {
  const match = deployments.find((item) => {
    const models = item.spec.models;
    const harnesses = item.spec.harnesses;
    return (
      deploymentKind(item.spec) === kind &&
      Array.isArray(models) &&
      models.includes(model) &&
      Array.isArray(harnesses) &&
      harnesses.includes(harness)
    );
  });
  if (!match)
    throw new Error(
      `no approved ${kind} deployment is available for ${model} and ${harness}`,
    );
  return match.alias;
}
