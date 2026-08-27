import {
  CheckCircle2,
  FileCode2,
  FlaskConical,
  LoaderCircle,
  Plus,
  RotateCcw,
  Square,
  TerminalSquare,
  Trash2,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  cancelWorkbenchSetup,
  getWorkbenchFile,
  getWorkbenchLogs,
  getWorkbenchSetup,
  listWorkbenchSetups,
  previewWorkbenchRecipe,
  startWorkbenchSetup,
  type WorkbenchFile,
  type WorkbenchPreview,
  type WorkbenchRecipe,
  type WorkbenchSetup,
} from "./api";
import { useControlState } from "./control-state";
import { PageHeader } from "./layout";
import { cn, formatDate } from "./lib";
import { Badge, Button, Card, ErrorNotice } from "./ui";

const sources = [
  "literal",
  "instruction_path",
  "workspace_path",
  "logs_path",
  "agent_home",
  "model_name",
  "model_base_url",
  "model_api_key",
] as const;

const fastAgentStarter: WorkbenchRecipe = {
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

const fxStarter: WorkbenchRecipe = {
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
    'url = f"https://releases.fx.sh/v0.0.5/fx-linux-{architecture}.tar.gz"',
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
    { name: "AI_GATEWAY_BASE_URL", source: "model_base_url" },
    { name: "AI_GATEWAY_API_KEY", source: "model_api_key" },
    { name: "OPENAI_BASE_URL", source: "model_base_url" },
    { name: "OPENAI_API_KEY", source: "model_api_key" },
    { name: "VERCEL_AI_GATEWAY_API_KEY", source: "model_api_key" },
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

function copyStarter(starter: WorkbenchRecipe = fastAgentStarter): WorkbenchRecipe {
  return structuredClone(starter);
}

function fieldClass(invalid = false): string {
  return cn(
    "w-full rounded-lg border bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none transition placeholder:text-slate-600 focus:ring-2 focus:ring-cyan-400",
    invalid ? "border-rose-500" : "border-slate-700",
  );
}

function statusTone(status: WorkbenchSetup["status"]): string {
  if (status === "passed") return "complete";
  if (status === "cancelled" || status === "failed" || status === "timed-out")
    return "error";
  return "active";
}

export function WorkbenchPage() {
  const { writeMode } = useControlState();
  const [recipe, setRecipe] = useState<WorkbenchRecipe>(copyStarter);
  const [preview, setPreview] = useState<WorkbenchPreview | null>(null);
  const [previewError, setPreviewError] = useState<unknown>(null);
  const [checking, setChecking] = useState(false);
  const [setup, setSetup] = useState<WorkbenchSetup | null>(null);
  const [setupError, setSetupError] = useState<unknown>(null);
  const [cancelling, setCancelling] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [logs, setLogs] = useState({ stdout: "", stderr: "" });
  const [selectedFile, setSelectedFile] = useState<WorkbenchFile | null>(null);
  const [fileContent, setFileContent] = useState<{
    content: string;
    truncated: boolean;
  } | null>(null);
  const [fileError, setFileError] = useState<unknown>(null);
  const previewSequence = useRef(0);
  const activeSetupRef = useRef<HTMLDivElement | null>(null);
  const liveOutputRef = useRef<HTMLElement | null>(null);
  const liveOutput = `${logs.stdout}${logs.stderr ? `\n[stderr]\n${logs.stderr}` : ""}`;

  useEffect(() => {
    void listWorkbenchSetups()
      .then((setups) => {
        if (setups[0]) setSetup(setups[0]);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const sequence = ++previewSequence.current;
    setChecking(true);
    setPreviewError(null);
    const timer = window.setTimeout(() => {
      void previewWorkbenchRecipe(recipe)
        .then((value) => {
          if (sequence !== previewSequence.current) return;
          setPreview(value);
        })
        .catch((error: unknown) => {
          if (sequence !== previewSequence.current) return;
          setPreview(null);
          setPreviewError(error);
        })
        .finally(() => {
          if (sequence === previewSequence.current) setChecking(false);
        });
    }, 350);
    return () => window.clearTimeout(timer);
  }, [recipe]);

  useEffect(() => {
    if (!setup || !["queued", "running", "cancelling"].includes(setup.status)) return;
    const timer = window.setInterval(() => {
      void getWorkbenchSetup(setup.setup_test_id).then(setSetup).catch(setSetupError);
      void getWorkbenchLogs(setup.setup_test_id).then(setLogs).catch(setSetupError);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [setup]);

  useEffect(() => {
    if (!setup) return;
    void getWorkbenchLogs(setup.setup_test_id).then(setLogs).catch(setSetupError);
  }, [setup]);

  useEffect(() => {
    if (!liveOutput) return;
    const output = liveOutputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }, [liveOutput]);

  const setupActive =
    setup !== null && ["queued", "running", "cancelling"].includes(setup.status);
  const verifiedCurrent =
    setup?.status === "passed" &&
    preview?.recipe_digest === setup.recipe_digest &&
    preview?.revision_id === setup.revision_id;

  const updateEnvironment = (
    index: number,
    change: Partial<WorkbenchRecipe["environment"][number]>,
  ) => {
    setRecipe((current) => {
      const environment = [...current.environment];
      const previous = environment[index];
      if (!previous) return current;
      const next = { ...previous, ...change };
      if (next.source !== "literal") delete next.value;
      else if (next.value === undefined) next.value = "";
      environment[index] = next;
      return { ...current, environment };
    });
    setConfirmed(false);
  };

  const launchSetup = async () => {
    setSetupError(null);
    try {
      const value = await startWorkbenchSetup(recipe);
      setSetup(value);
      setConfirmed(false);
      setSelectedFile(null);
      setFileContent(null);
      setLogs({ stdout: "", stderr: "" });
      window.requestAnimationFrame(() => {
        const target = activeSetupRef.current;
        if (typeof target?.scrollIntoView === "function")
          target.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
    } catch (error) {
      setSetupError(error);
    }
  };

  const cancelSetup = async () => {
    if (!setup || !setupActive) return;
    if (
      !window.confirm(
        "Cancel this setup test? The disposable setup environment will be stopped.",
      )
    )
      return;
    setCancelling(true);
    setSetupError(null);
    try {
      setSetup(await cancelWorkbenchSetup(setup.setup_test_id));
    } catch (error) {
      setSetupError(error);
    } finally {
      setCancelling(false);
    }
  };

  const selectFile = async (file: WorkbenchFile) => {
    if (!setup) return;
    setSelectedFile(file);
    setFileContent(null);
    setFileError(null);
    if (!file.text) return;
    try {
      setFileContent(await getWorkbenchFile(setup.setup_test_id, file.file_id));
    } catch (error) {
      setFileError(error);
    }
  };

  return (
    <>
      <PageHeader
        title="Agent Workbench"
        description="Configure and privately verify a command-line agent. The service previews the exact immutable recipe before executing setup in a disposable CPU sandbox."
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                setRecipe(copyStarter());
                setSetup(null);
                setConfirmed(false);
              }}
            >
              <RotateCcw size={16} /> Fast-Agent 0.10.11
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                setRecipe(copyStarter(fxStarter));
                setSetup(null);
                setConfirmed(false);
              }}
            >
              <RotateCcw size={16} /> FX 0.0.5
            </Button>
          </div>
        }
      />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.15fr)_minmax(22rem,0.85fr)]">
        <div className="space-y-6">
          <Card>
            <div className="mb-5 flex items-center gap-3">
              <TerminalSquare className="text-cyan-300" size={20} />
              <div>
                <h2 className="font-semibold text-white">Commands</h2>
                <p className="text-sm text-slate-400">
                  Environment references stay visible and are resolved by the server.
                </p>
              </div>
            </div>
            <div className="space-y-5">
              <label className="block">
                <span className="mb-2 block text-sm font-medium text-slate-200">
                  Configuration name
                </span>
                <input
                  className={fieldClass()}
                  value={recipe.name}
                  onChange={(event) =>
                    setRecipe((current) => ({
                      ...current,
                      name: event.target.value,
                    }))
                  }
                />
              </label>
              <label className="block">
                <span className="mb-2 block text-sm font-medium text-slate-200">
                  Setup command
                </span>
                <textarea
                  aria-label="Setup command"
                  className={cn(fieldClass(), "min-h-40 font-mono leading-6")}
                  spellCheck={false}
                  value={recipe.setup_command}
                  onChange={(event) =>
                    setRecipe((current) => ({
                      ...current,
                      setup_command: event.target.value,
                    }))
                  }
                />
                <span className="mt-1 block text-xs text-slate-500">
                  Runs without model credentials in a disposable setup environment.
                </span>
              </label>
              <label className="block">
                <span className="mb-2 block text-sm font-medium text-slate-200">
                  Run command
                </span>
                <textarea
                  aria-label="Run command"
                  className={cn(fieldClass(), "min-h-56 font-mono leading-6")}
                  spellCheck={false}
                  value={recipe.run_command}
                  onChange={(event) =>
                    setRecipe((current) => ({
                      ...current,
                      run_command: event.target.value,
                    }))
                  }
                />
              </label>
              <label className="block max-w-xs">
                <span className="mb-2 block text-sm font-medium text-slate-200">
                  Inference API
                </span>
                <select
                  aria-label="Inference API"
                  className={fieldClass()}
                  value={recipe.route_api}
                  onChange={(event) =>
                    setRecipe((current) => ({
                      ...current,
                      route_api: event.target.value as WorkbenchRecipe["route_api"],
                    }))
                  }
                >
                  <option value="chat-completions">Chat Completions</option>
                  <option value="responses">Responses</option>
                </select>
                <span className="mt-1 block text-xs text-slate-500">
                  Must match the OpenAI-compatible protocol expected by the agent.
                </span>
              </label>
              <label className="block max-w-xs">
                <span className="mb-2 block text-sm font-medium text-slate-200">
                  Setup timeout
                </span>
                <div className="flex items-center gap-2">
                  <input
                    className={fieldClass()}
                    min={30}
                    max={3600}
                    type="number"
                    value={recipe.setup_timeout_seconds}
                    onChange={(event) =>
                      setRecipe((current) => ({
                        ...current,
                        setup_timeout_seconds: Number(event.target.value),
                      }))
                    }
                  />
                  <span className="text-sm text-slate-500">seconds</span>
                </div>
              </label>
            </div>
          </Card>
        </div>

        <div className="space-y-6">
          <Card>
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h2 className="font-semibold text-white">Authoritative preview</h2>
                <p className="mt-1 text-sm text-slate-400">
                  Generated by the same compiler that locks the recipe.
                </p>
              </div>
              {checking ? (
                <LoaderCircle className="animate-spin text-cyan-300" size={18} />
              ) : preview ? (
                <Badge status="complete">Preview ready</Badge>
              ) : (
                <Badge status="error">Invalid</Badge>
              )}
            </div>
            {previewError ? <ErrorNotice error={previewError} /> : null}
            {preview ? (
              <div className="space-y-4">
                <div className="rounded-lg bg-slate-900 p-3 text-xs text-slate-400">
                  <div>
                    Revision:{" "}
                    <code className="text-slate-200">{preview.revision_id}</code>
                  </div>
                  <div className="mt-1 break-all">
                    Digest:{" "}
                    <code className="text-slate-200">{preview.recipe_digest}</code>
                  </div>
                </div>
                <div>
                  <h3 className="mb-2 text-sm font-medium text-slate-200">
                    Expanded setup
                  </h3>
                  <pre className="max-h-64 overflow-auto rounded-lg border border-slate-800 bg-black/30 p-3 text-xs leading-5 text-slate-300">
                    {preview.setup_command}
                  </pre>
                </div>
                <div>
                  <h3 className="mb-2 text-sm font-medium text-slate-200">
                    Expanded run command
                  </h3>
                  <pre className="max-h-80 overflow-auto rounded-lg border border-slate-800 bg-black/30 p-3 text-xs leading-5 text-slate-300">
                    {preview.run_command}
                  </pre>
                </div>
                <div>
                  <h3 className="mb-2 text-sm font-medium text-slate-200">
                    Effective environment
                  </h3>
                  <div className="space-y-1 rounded-lg border border-slate-800 p-3 font-mono text-xs">
                    {preview.environment.map((item) => (
                      <div className="break-all" key={item.name}>
                        <span className="text-cyan-300">{item.name}</span>
                        <span className="text-slate-600">=</span>
                        <span className="text-slate-300">
                          {item.redacted ? "<injected placeholder>" : item.value}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            ) : null}
          </Card>

          <Card>
            <div className="mb-4 flex items-center gap-3">
              <FlaskConical className="text-cyan-300" size={20} />
              <div>
                <h2 className="font-semibold text-white">Try setup</h2>
                <p className="text-sm text-slate-400">
                  Installs the agent without running a benchmark or model request.
                </p>
              </div>
            </div>
            {setupError ? <ErrorNotice error={setupError} /> : null}
            {setup && !verifiedCurrent && setup.status === "passed" ? (
              <p className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
                Setup verified an older recipe. Run setup again to test the current
                edits.
              </p>
            ) : null}
            {setupActive ? (
              <div
                className="space-y-4 rounded-lg border border-cyan-500/30 bg-cyan-500/5 p-4"
                ref={activeSetupRef}
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <LoaderCircle className="animate-spin text-cyan-300" size={18} />
                    <span className="font-medium text-white">Setup submitted</span>
                    <Badge status="active">{setup.status}</Badge>
                  </div>
                  <Button
                    disabled={cancelling || setup.status === "cancelling"}
                    variant="secondary"
                    onClick={() => void cancelSetup()}
                  >
                    <Square size={14} />
                    {setup.status === "cancelling" || cancelling
                      ? "Cancelling…"
                      : "Cancel setup"}
                  </Button>
                </div>
                <p className="text-xs text-slate-400">
                  Confirmation was reset for any future retry. This setup continues
                  below and can be safely cancelled here.
                </p>
                <section
                  aria-label="Live setup output"
                  className="max-h-64 min-h-32 overflow-auto rounded-lg border border-slate-800 bg-black/40 p-3 text-xs leading-5 text-slate-300"
                  ref={liveOutputRef}
                >
                  <pre className="whitespace-pre-wrap break-words">
                    {liveOutput || "Waiting for setup output…"}
                  </pre>
                </section>
              </div>
            ) : (
              <>
                <label className="flex items-start gap-3 text-sm text-slate-300">
                  <input
                    className="mt-1"
                    type="checkbox"
                    checked={confirmed}
                    onChange={(event) => setConfirmed(event.target.checked)}
                  />
                  <span>
                    Launch this exact setup recipe in a disposable CPU sandbox.
                  </span>
                </label>
                <Button
                  className="mt-4 w-full"
                  disabled={
                    writeMode !== "enabled" || !confirmed || !preview || checking
                  }
                  onClick={() => void launchSetup()}
                >
                  <FlaskConical size={16} /> Launch setup test
                </Button>
              </>
            )}
          </Card>
        </div>

        <Card className="xl:col-span-2">
          <div className="mb-4 flex items-center justify-between gap-4">
            <div>
              <h2 className="font-semibold text-white">Environment</h2>
              <p className="mt-1 text-sm text-slate-400">
                Select runtime bindings instead of pasting credentials.
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() =>
                setRecipe((current) => ({
                  ...current,
                  environment: [
                    ...current.environment,
                    { name: "NEW_VALUE", source: "literal", value: "" },
                  ],
                }))
              }
            >
              <Plus size={15} /> Add
            </Button>
          </div>
          <div className="grid gap-3 xl:grid-cols-2">
            {recipe.environment.map((binding, index) => (
              <div
                className="relative grid gap-3 rounded-lg border border-slate-800 p-3 pr-12 sm:grid-cols-2"
                // biome-ignore lint/suspicious/noArrayIndexKey: the portable recipe contract intentionally has no UI-only row identifier.
                key={`${index}-${binding.name}`}
              >
                <label>
                  <span className="mb-1 block text-xs text-slate-500">Name</span>
                  <input
                    aria-label={`Environment variable ${index + 1} name`}
                    className={fieldClass()}
                    value={binding.name}
                    onChange={(event) =>
                      updateEnvironment(index, { name: event.target.value })
                    }
                  />
                </label>
                <label>
                  <span className="mb-1 block text-xs text-slate-500">Source</span>
                  <select
                    aria-label={`Environment variable ${binding.name} source`}
                    className={fieldClass()}
                    value={binding.source}
                    onChange={(event) =>
                      updateEnvironment(index, {
                        source: event.target
                          .value as WorkbenchRecipe["environment"][number]["source"],
                      })
                    }
                  >
                    {sources.map((source) => (
                      <option key={source} value={source}>
                        {source.replaceAll("_", " ")}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="sm:col-span-2">
                  <span className="mb-1 block text-xs text-slate-500">
                    {binding.source === "literal" ? "Value" : "Effective binding"}
                  </span>
                  <input
                    aria-label={`Environment variable ${binding.name} value`}
                    className={fieldClass()}
                    disabled={binding.source !== "literal"}
                    value={
                      binding.source === "literal"
                        ? (binding.value ?? "")
                        : `<${binding.source.replaceAll("_", " ")}>`
                    }
                    onChange={(event) =>
                      updateEnvironment(index, { value: event.target.value })
                    }
                  />
                </label>
                <Button
                  aria-label={`Remove ${binding.name}`}
                  className="absolute right-2 top-2"
                  variant="ghost"
                  onClick={() =>
                    setRecipe((current) => ({
                      ...current,
                      environment: current.environment.filter(
                        (_item, itemIndex) => itemIndex !== index,
                      ),
                    }))
                  }
                >
                  <Trash2 size={16} />
                </Button>
              </div>
            ))}
          </div>
        </Card>
      </div>

      {setup ? (
        <div className="mt-6 space-y-6">
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <div className="flex items-center gap-2">
                  {setup.status === "passed" ? (
                    <CheckCircle2 className="text-emerald-400" size={20} />
                  ) : ["running", "queued", "cancelling"].includes(setup.status) ? (
                    <LoaderCircle className="animate-spin text-cyan-300" size={20} />
                  ) : (
                    <FlaskConical className="text-rose-400" size={20} />
                  )}
                  <h2 className="font-semibold text-white">Setup test</h2>
                  <Badge status={statusTone(setup.status)}>{setup.status}</Badge>
                </div>
                <p className="mt-2 text-sm text-slate-400">
                  Started {setup.started_at ? formatDate(setup.started_at) : "soon"}
                  {setup.completed_at
                    ? ` · finished ${formatDate(setup.completed_at)}`
                    : ""}
                </p>
              </div>
              {verifiedCurrent ? (
                <div className="max-w-sm text-right">
                  <Button disabled variant="secondary">
                    Benchmark handoff unavailable <FileCode2 size={16} />
                  </Button>
                  <p className="mt-2 text-xs text-slate-500">
                    This build verifies setup only. It does not yet bind the recipe to a
                    benchmark Run.
                  </p>
                </div>
              ) : null}
            </div>
            {setup.error ? (
              <p className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-200">
                {setup.error}
              </p>
            ) : null}
          </Card>

          <div className="grid gap-6 xl:grid-cols-2">
            <Card>
              <h2 className="font-semibold text-white">Setup logs</h2>
              <div className="mt-4 space-y-4">
                <div>
                  <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">
                    Standard output
                  </h3>
                  <section
                    aria-label="Setup standard output"
                    className="max-h-[32rem] min-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-800 bg-black/30 p-3 text-xs leading-5 text-slate-300"
                  >
                    <pre>{logs.stdout || "Waiting for output…"}</pre>
                  </section>
                </div>
                {logs.stderr ? (
                  <div>
                    <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">
                      Standard error
                    </h3>
                    <section
                      aria-label="Setup standard error"
                      className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-rose-900/60 bg-rose-950/20 p-3 text-xs leading-5 text-rose-200"
                    >
                      <pre>{logs.stderr}</pre>
                    </section>
                  </div>
                ) : null}
              </div>
            </Card>

            <Card>
              <h2 className="font-semibold text-white">Created files</h2>
              <p className="mt-1 text-sm text-slate-400">
                Text previews are escaped and bounded. Binary files are listed only.
              </p>
              <div className="mt-4 grid min-h-72 gap-4 md:grid-cols-[minmax(12rem,0.8fr)_minmax(0,1.2fr)]">
                <div className="max-h-[32rem] overflow-auto rounded-lg border border-slate-800 p-2">
                  {setup.files.length === 0 ? (
                    <p className="p-3 text-sm text-slate-500">
                      Files appear after setup completes.
                    </p>
                  ) : (
                    setup.files.map((file) => (
                      <button
                        className={cn(
                          "block w-full rounded px-2 py-1.5 text-left text-xs hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400",
                          selectedFile?.file_id === file.file_id
                            ? "bg-cyan-400/10 text-cyan-200"
                            : "text-slate-300",
                        )}
                        key={file.file_id}
                        onClick={() => void selectFile(file)}
                        type="button"
                      >
                        <span className="text-slate-500">{file.root}/</span>
                        {file.path}
                        <span className="ml-2 text-slate-600">{file.size} B</span>
                      </button>
                    ))
                  )}
                </div>
                <div className="min-w-0 rounded-lg border border-slate-800 bg-black/20 p-3">
                  {fileError ? <ErrorNotice error={fileError} /> : null}
                  {!selectedFile ? (
                    <p className="text-sm text-slate-500">
                      Select a file to inspect it.
                    </p>
                  ) : !selectedFile.text ? (
                    <p className="text-sm text-slate-400">
                      Binary file · {selectedFile.size} bytes
                    </p>
                  ) : fileContent ? (
                    <>
                      <section
                        aria-label={`Contents of ${selectedFile.path}`}
                        className="max-h-[30rem] overflow-auto whitespace-pre-wrap break-words text-xs leading-5 text-slate-300"
                      >
                        <pre>{fileContent.content}</pre>
                      </section>
                      {fileContent.truncated ? (
                        <p className="mt-2 text-xs text-amber-300">
                          Preview truncated.
                        </p>
                      ) : null}
                    </>
                  ) : (
                    <p className="text-sm text-slate-500">Loading preview…</p>
                  )}
                </div>
              </div>
            </Card>
          </div>
        </div>
      ) : null}
    </>
  );
}
