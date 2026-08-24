import type { DeploymentProfileSpec } from "@harbor-hf/contracts";
import { sha256 } from "@harbor-hf/contracts";

const ID_PATTERN = /^[a-z0-9][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
const MAX_ID_LENGTH = 160;
const UNIQUE_HEX_LENGTH = 12;

/**
 * Compact a profile alias or effort label into an Id-safe hyphenated segment.
 */
export function slugSegment(value: string): string {
  const slug = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!slug) throw new Error("run identity segment is empty");
  return slug;
}

/**
 * Classify the locked runtime as providers, endpoints, or none.
 *
 * Inference Providers set `inference_provider` or a router upstream. Managed
 * Endpoints use `endpoints.huggingface.cloud`. Control-smoke and other local
 * Jobs have neither.
 */
export function runtimeKind(
  spec: DeploymentProfileSpec,
): "providers" | "endpoints" | "none" {
  if (spec.route !== "hf_job") return "none";
  if (typeof spec.inference_provider === "string" && spec.inference_provider.length > 0)
    return "providers";
  const upstream = spec.trial_job_template?.inference_upstream;
  if (typeof upstream !== "string" || upstream.length === 0) return "none";
  if (upstream.includes("router.huggingface.co")) return "providers";
  if (upstream.includes("endpoints.huggingface.cloud")) return "endpoints";
  return "none";
}

/**
 * Digest of the operator, namespace, and idempotency key.
 *
 * The readable prefix names the combo. This suffix keeps two launches of the
 * same combo distinct and makes the same key resolve to the same run.
 */
export function runUnique(namespace: string, actor: string, keyDigest: string): string {
  return sha256([namespace, actor, keyDigest].join("\u0000")).slice(
    "sha256:".length,
    "sha256:".length + UNIQUE_HEX_LENGTH,
  );
}

/**
 * Build `run-<model>-<harness>-<reasoning>-<runtime>-<unique>`.
 */
export function runIdentity(input: {
  model: string;
  harness: string;
  reasoning: string;
  runtime: string;
  unique: string;
}): string {
  const prefix = [
    "run",
    slugSegment(input.model),
    slugSegment(input.harness),
    slugSegment(input.reasoning),
    slugSegment(input.runtime),
  ].join("-");
  const suffix = `-${slugSegment(input.unique)}`;
  const maxPrefix = MAX_ID_LENGTH - suffix.length;
  const clipped =
    prefix.length > maxPrefix ? prefix.slice(0, maxPrefix).replace(/-+$/g, "") : prefix;
  const id = `${clipped}${suffix}`;
  if (id.length > MAX_ID_LENGTH || !ID_PATTERN.test(id))
    throw new Error(`run identity is invalid: ${id}`);
  return id;
}
