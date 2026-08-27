import type { AgentWorkbenchRecipeV1, HarnessProfileSpec } from "@harbor-hf/contracts";
import {
  canonicalJson,
  deterministicId,
  sha256,
  validateAgentWorkbenchRecipe,
} from "@harbor-hf/contracts";

const reservedEnvironment = new Set([
  "BASH_ENV",
  "ENV",
  "HOME",
  "LD_PRELOAD",
  "PATH",
  "PYTHONPATH",
  "SHELL",
  "HF_TOKEN",
  "HF_INFERENCE_TOKEN",
  "HARBOR_HF_WORKER_CAPABILITY",
]);

const secretName = /(?:^|_)(?:API_?KEY|ACCESS_?TOKEN|AUTH_?TOKEN|PASSWORD|SECRET)$/i;
const suspiciousLiteral =
  /(?:\bBearer\s+[A-Za-z0-9._~+/-]{12,}|hf[_-][A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{12,})/i;

export const workbenchRuntimeValues = {
  instruction_path: "/run/agent/instruction.txt",
  workspace_path: "/app",
  logs_path: "/logs/agent",
  agent_home: "/logs/agent/home",
  model_name: "<locked-model-route>",
  model_base_url: "http://127.0.0.1:18080/v1",
  model_api_key: "<injected-placeholder>",
} as const;

export interface WorkbenchPreviewEnvironment {
  name: string;
  source: AgentWorkbenchRecipeV1["environment"][number]["source"];
  value: string;
  redacted: boolean;
}

export interface AgentWorkbenchPreview {
  recipe: AgentWorkbenchRecipeV1;
  recipe_digest: string;
  revision_id: string;
  setup_command: string;
  run_command: string;
  environment: WorkbenchPreviewEnvironment[];
  harness_profile: HarnessProfileSpec;
  warnings: string[];
}

export const fastAgentWorkbenchStarter: AgentWorkbenchRecipeV1 = {
  schema_version: "v1",
  name: "fast-agent",
  setup_command: [
    'python -m venv "$AGENT_HOME/venv"',
    '"$AGENT_HOME/venv/bin/pip" install --no-cache-dir fast-agent-mcp==0.10.11',
    '"$AGENT_HOME/venv/bin/fast-agent" --version',
  ].join("\n"),
  run_command: [
    '"$AGENT_HOME/venv/bin/fast-agent" go',
    '  --model "$AGENT_MODEL"',
    '  --base-url "$MODEL_BASE_URL"',
    '  --prompt-file "$TASK_INSTRUCTION_PATH"',
    '  --workspace "$TASK_WORKSPACE"',
    '  --home "$AGENT_HOME/runtime"',
    '  --results "$AGENT_RESULTS_PATH"',
    '  --trajectory-output "$AGENT_TRAJECTORY_PATH"',
    "  --shell",
    "  --quiet",
  ].join(" \\\n"),
  route_api: "chat-completions",
  setup_timeout_seconds: 1800,
  environment: [
    { name: "AGENT_HOME", source: "agent_home" },
    { name: "AGENT_MODEL", source: "model_name" },
    { name: "GENERIC_API_KEY", source: "model_api_key" },
    { name: "MODEL_BASE_URL", source: "model_base_url" },
    {
      name: "AGENT_RESULTS_PATH",
      source: "literal",
      value: "/logs/agent/fast-agent-results.json",
    },
    {
      name: "AGENT_TRAJECTORY_PATH",
      source: "literal",
      value: "/logs/agent/trajectory.json",
    },
    { name: "TASK_INSTRUCTION_PATH", source: "instruction_path" },
    { name: "TASK_WORKSPACE", source: "workspace_path" },
  ],
  outputs: {
    results_path: "/logs/agent/fast-agent-results.json",
    trajectory_path: "/logs/agent/trajectory.json",
  },
};

export const fxWorkbenchStarter: AgentWorkbenchRecipeV1 = {
  schema_version: "v1",
  name: "fx",
  setup_command: [
    'mkdir -p "$AGENT_HOME/bin"',
    "python - <<'PY'",
    "import os",
    "import platform",
    "import tarfile",
    "import tempfile",
    "from pathlib import Path",
    "from urllib.request import urlopen",
    "",
    'architecture = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}[platform.machine().lower()]',
    'url = f"https://releases.fx.sh/v0.0.6/fx-linux-{architecture}.tar.gz"',
    'destination = Path(os.environ["AGENT_HOME"]) / "bin" / "fx"',
    "with tempfile.TemporaryDirectory() as directory:",
    '    archive = Path(directory) / "fx.tar.gz"',
    "    archive.write_bytes(urlopen(url, timeout=60).read())",
    '    with tarfile.open(archive, "r:gz") as bundle:',
    '        bundle.extract("fx", path=directory, filter="data")',
    '    destination.write_bytes((Path(directory) / "fx").read_bytes())',
    "destination.chmod(0o755)",
    "PY",
    '"$AGENT_HOME/bin/fx" --version',
  ].join("\n"),
  run_command: [
    'cd "$TASK_WORKSPACE"',
    '"$AGENT_HOME/bin/fx" ask --yolo --json -- "$(cat "$TASK_INSTRUCTION_PATH")"',
    '  > "$AGENT_RESULTS_PATH"',
  ].join(" \\\n"),
  route_api: "chat-completions",
  setup_timeout_seconds: 600,
  environment: [
    { name: "AGENT_HOME", source: "agent_home" },
    { name: "FX_MODEL", source: "model_name" },
    { name: "AI_GATEWAY_API_KEY", source: "model_api_key" },
    { name: "FX_AUTO_UPGRADE", source: "literal", value: "0" },
    {
      name: "AGENT_RESULTS_PATH",
      source: "literal",
      value: "/logs/agent/fx-results.json",
    },
    { name: "TASK_INSTRUCTION_PATH", source: "instruction_path" },
    { name: "TASK_WORKSPACE", source: "workspace_path" },
  ],
  outputs: {
    results_path: "/logs/agent/fx-results.json",
    trajectory_path: null,
  },
};

function quoteShell(value: string): string {
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value;
  return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

function expandSimpleEnvironment(
  command: string,
  environment: ReadonlyMap<string, string>,
): string {
  return command.replace(
    /\$(?:\{([A-Z_][A-Z0-9_]*)\}|([A-Z_][A-Z0-9_]*))/g,
    (match, braced: string | undefined, plain: string | undefined) => {
      const name = braced ?? plain;
      if (!name || !environment.has(name)) return match;
      return quoteShell(environment.get(name) as string);
    },
  );
}

function validateRecipeSemantics(recipe: AgentWorkbenchRecipeV1): void {
  if (recipe.setup_command.includes("\0") || recipe.run_command.includes("\0"))
    throw new Error("commands must not contain NUL characters");
  if (
    suspiciousLiteral.test(recipe.setup_command) ||
    suspiciousLiteral.test(recipe.run_command)
  )
    throw new Error("commands must not contain credential-like values");
  const names = new Set<string>();
  const sources = new Map<
    string,
    AgentWorkbenchRecipeV1["environment"][number]["source"]
  >();
  for (const binding of recipe.environment) {
    if (names.has(binding.name))
      throw new Error(`environment variable ${binding.name} is duplicated`);
    names.add(binding.name);
    sources.set(binding.name, binding.source);
    if (reservedEnvironment.has(binding.name) || binding.name.startsWith("HARBOR_HF_"))
      throw new Error(`environment variable ${binding.name} is reserved`);
    if (binding.source === "literal") {
      if (binding.value === undefined)
        throw new Error(`literal environment variable ${binding.name} needs a value`);
      if (secretName.test(binding.name))
        throw new Error(
          `literal environment variable ${binding.name} looks credential-like`,
        );
      if (suspiciousLiteral.test(binding.value ?? ""))
        throw new Error(
          `literal environment variable ${binding.name} looks like a credential`,
        );
    } else if (binding.value !== undefined)
      throw new Error(
        `runtime environment variable ${binding.name} must not declare a literal value`,
      );
  }
  const setupOnlyUnavailable = new Set([
    "instruction_path",
    "model_base_url",
    "model_api_key",
  ]);
  for (const match of recipe.setup_command.matchAll(
    /\$(?:\{([A-Z_][A-Z0-9_]*)\}|([A-Z_][A-Z0-9_]*))/g,
  )) {
    const name = match[1] ?? match[2];
    const source = name ? sources.get(name) : undefined;
    if (source && setupOnlyUnavailable.has(source))
      throw new Error(`setup command cannot use run-only environment variable ${name}`);
  }
  for (const path of [
    recipe.outputs.results_path,
    recipe.outputs.trajectory_path,
  ].filter((value): value is string => value !== null)) {
    if (path.includes("..") || path.includes("//"))
      throw new Error("output paths must remain beneath /logs/agent");
  }
}

function commandAgentConfig(recipe: AgentWorkbenchRecipeV1): Record<string, unknown> {
  const bindings: Record<string, string> = {};
  const literals: Record<string, string> = {};
  const bindingNames = {
    instruction_path: "instruction_path",
    workspace_path: "workspace_path",
    logs_path: "logs_path",
    agent_home: "agent_home",
    model_name: "model_name",
    model_base_url: "route_base_url",
    model_api_key: "route_api_key",
  } as const;
  for (const item of recipe.environment) {
    if (item.source === "literal") literals[item.name] = item.value ?? "";
    else bindings[item.name] = bindingNames[item.source];
  }
  return {
    schema_version: "v1",
    setup: {
      script: recipe.setup_command,
      bindings: Object.fromEntries(
        Object.entries(bindings).filter(([, value]) =>
          ["workspace_path", "logs_path", "agent_home", "model_name"].includes(value),
        ),
      ),
      literals,
    },
    run: {
      script: recipe.run_command,
      bindings,
      literals,
    },
    route_api: recipe.route_api,
    outputs: [
      {
        path: recipe.outputs.results_path.replace(/^\/logs\/agent\//, ""),
      },
    ],
    ...(recipe.outputs.trajectory_path
      ? {
          atif: {
            path: recipe.outputs.trajectory_path.replace(/^\/logs\/agent\//, ""),
          },
        }
      : {}),
  };
}

export function compileAgentWorkbenchRecipe(value: unknown): AgentWorkbenchPreview {
  const recipe = validateAgentWorkbenchRecipe<AgentWorkbenchRecipeV1>(
    structuredClone(value),
  );
  validateRecipeSemantics(recipe);
  const environment = recipe.environment
    .map((binding) => {
      const redacted = binding.source === "model_api_key";
      return {
        name: binding.name,
        source: binding.source,
        value:
          binding.source === "literal"
            ? (binding.value ?? "")
            : workbenchRuntimeValues[binding.source],
        redacted,
      };
    })
    .sort((left, right) => left.name.localeCompare(right.name));
  const values = new Map(environment.map((item) => [item.name, item.value]));
  const recipeDigest = sha256(canonicalJson(recipe));
  const runSources = new Set(recipe.environment.map((binding) => binding.source));
  const requiredEvidence = ["workspace", "verifier"];
  if (runSources.has("model_base_url") || runSources.has("model_api_key"))
    requiredEvidence.push("provider-usage");
  if (recipe.outputs.trajectory_path) requiredEvidence.push("trajectory");
  return {
    recipe,
    recipe_digest: recipeDigest,
    revision_id: deterministicId("agent-recipe", recipe.name, recipeDigest),
    setup_command: expandSimpleEnvironment(recipe.setup_command, values),
    run_command: expandSimpleEnvironment(recipe.run_command, values),
    environment,
    harness_profile: {
      agent: "command-agent",
      revision: recipeDigest,
      reasoning_effort: "off",
      required_evidence: requiredEvidence,
      harbor_agent: {
        import_path: "harbor_hf_agents.command_agent.agent:CommandAgent",
        override_setup_timeout_sec: recipe.setup_timeout_seconds,
        kwargs: {
          config: commandAgentConfig(recipe),
        },
      },
    },
    warnings: recipe.outputs.trajectory_path
      ? []
      : ["No ATIF trajectory is declared; results remain diagnostic."],
  };
}
