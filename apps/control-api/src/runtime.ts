import { existsSync } from "node:fs";
import type { OperatorAcl } from "@harbor-hf/contracts";
import { deterministicId, sha256 } from "@harbor-hf/contracts";
import {
  ControlService,
  FilesystemObjectStore,
  type ImmutableObjectStore,
  type LoadedProfile,
  loadBuiltInProfiles,
  Projection,
  Reconciler,
  ResultPublisher,
} from "@harbor-hf/control-core";
import {
  attestInferenceToken,
  HuggingFaceActions,
  HuggingFaceBucketStore,
  type HuggingFaceSandboxGateway,
  NoopActions,
} from "@harbor-hf/hf-adapters";
import { AuthenticationService, AuthStore } from "./auth.js";
import type { AppConfig } from "./config.js";

export interface Runtime {
  config: AppConfig;
  projection: Projection;
  store: ImmutableObjectStore;
  service: ControlService;
  auth: AuthenticationService;
  sandboxes: HuggingFaceSandboxGateway | null;
  reconciler: Reconciler;
  initialize(): Promise<void>;
  start(): void;
  close(): Promise<void>;
}

function builtInProfileId(
  profiles: readonly LoadedProfile[],
  kind: LoadedProfile["profile"]["profile_kind"],
  name: string,
): string {
  const matches = profiles.filter(
    (item) => item.profile.profile_kind === kind && item.profile.name === name,
  );
  if (matches.length !== 1) {
    throw new Error(`expected one built-in ${kind} canary profile`);
  }
  return (matches[0] as LoadedProfile).profile_id;
}

function canaryProfilesAllowed(
  profiles: readonly LoadedProfile[],
): (resolved: readonly { kind: string; profile_id: string }[]) => boolean {
  const required = new Map([
    ["benchmark", builtInProfileId(profiles, "benchmark", "control-smoke")],
    ["model", builtInProfileId(profiles, "model", "control-smoke")],
    ["harness", builtInProfileId(profiles, "harness", "control-smoke")],
    ["launch_policy", builtInProfileId(profiles, "launch_policy", "control-smoke")],
  ]);
  const deployments = new Set([
    builtInProfileId(profiles, "deployment", "hf-cpu-smoke"),
    builtInProfileId(profiles, "deployment", "hf-cpu-sandbox-smoke"),
  ]);
  return (resolved) => {
    if (resolved.length !== 5) return false;
    for (const [kind, profileId] of required) {
      if (
        !resolved.some(
          (profile) => profile.kind === kind && profile.profile_id === profileId,
        )
      )
        return false;
    }
    return resolved.some(
      (profile) => profile.kind === "deployment" && deployments.has(profile.profile_id),
    );
  };
}

export async function createRuntime(config: AppConfig): Promise<Runtime> {
  if (config.store_mode === "filesystem" && !existsSync(config.bucket_root))
    throw new Error("filesystem object-store root is missing");
  const store: ImmutableObjectStore =
    config.store_mode === "bucket"
      ? new HuggingFaceBucketStore({
          bucketId: config.bucket_id,
          accessToken: config.hf_token ?? "",
        })
      : new FilesystemObjectStore(config.bucket_root);
  const projection = await Projection.open(config.projection_path);
  const profiles = await loadBuiltInProfiles(config.profiles_root);
  const canaryAllowed = canaryProfilesAllowed(profiles);
  const service = new ControlService(config.namespace, store, projection, profiles, {
    ...(config.write_mode === "canary"
      ? { campaignProfilesAllowed: canaryAllowed }
      : {}),
  });
  const authStore = await AuthStore.open(config.auth_path);
  const auth = new AuthenticationService(
    config.auth_mode,
    authStore,
    config.oauth,
    () => projection.latestAcl(),
  );
  const hfActions = config.hf_token
    ? new HuggingFaceActions({
        namespace: config.namespace,
        accessToken: config.hf_token,
        ...(config.hf_inference_token
          ? { inferenceToken: config.hf_inference_token }
          : {}),
        controlUrl: config.public_origin,
      })
    : null;
  const external = hfActions ?? new NoopActions();
  const publisher = new ResultPublisher(store, projection, service);
  const reconciler = new Reconciler(service, projection, external, publisher, {
    interval_ms: config.reconcile_interval_ms,
    observation_interval_ms: config.observe_interval_ms,
    worker_receipt_grace_ms: config.worker_receipt_grace_ms,
    batch_size: 16,
    ...(config.write_mode === "canary"
      ? {
          campaign_allowed: async (campaignId: string) => {
            const lock = await projection.campaignLock(campaignId);
            return lock ? canaryAllowed(lock.profiles) : false;
          },
        }
      : {}),
  });
  const abort = new AbortController();
  return {
    config,
    projection,
    store,
    service,
    auth,
    sandboxes: hfActions?.sandboxes ?? null,
    reconciler,
    async initialize() {
      if (config.hf_inference_token)
        await attestInferenceToken({ accessToken: config.hf_inference_token });
      await auth.initialize();
      await projection.rebuild(store);
      await service.initialize(profiles);
      if (
        !(await projection.latestAcl()) &&
        config.bootstrap_operator_subjects.length > 0
      ) {
        const operators = [...new Set(config.bootstrap_operator_subjects)].sort();
        const acl: OperatorAcl = {
          schema_version: "v1",
          kind: "operator.acl",
          record_id: deterministicId("operator-acl", sha256(operators.join("\u0000"))),
          created_at: new Date().toISOString(),
          actor: { subject: "harbor-hf-bootstrap", role: "migration" },
          operators,
          readers: [],
        };
        await service.append(acl);
      }
    },
    start() {
      if (config.write_mode !== "disabled") reconciler.start(abort.signal);
    },
    async close() {
      abort.abort();
      await reconciler.stop();
      authStore.close();
      await projection.close();
    },
  };
}
