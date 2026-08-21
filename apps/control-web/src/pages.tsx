import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  AlertTriangle,
  ArrowRight,
  CircleDollarSign,
  Clock3,
  ExternalLink,
  Gauge,
  PauseCircle,
  PlayCircle,
  Plus,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { z } from "zod";
import {
  type AuditResponse,
  actOnCampaign,
  type CampaignAction,
  type CampaignList,
  type CampaignSubmission,
  type EndpointList,
  type JobList,
  type ProfileList,
  type ResultDetail,
  type ResultList,
  submitCampaign,
  type TaskList,
} from "./api";
import { DataTable } from "./components/data-table";
import { useControlState } from "./control-state";
import { hints } from "./hints";
import { PageHeader } from "./layout";
import {
  estimateLaunchReservationMicrousd,
  formatDate,
  formatMoney,
  humanize,
  shortId,
} from "./lib";
import {
  keys,
  useAudit,
  useCampaign,
  useCampaigns,
  useEndpoints,
  useJobs,
  useProfiles,
  useResult,
  useResults,
  useSystem,
  useTask,
  useTasks,
} from "./queries";
import { Badge, Button, Card, Empty, Hint, Progress, QueryContent } from "./ui";

type CampaignRow = CampaignList["items"][number];
type TaskRow = TaskList["items"][number];
type JobRow = JobList["items"][number];
type EndpointRow = EndpointList["items"][number];
type ProfileRow = ProfileList["items"][number];
type ResultRow = ResultList["items"][number];
type AuditRow = AuditResponse["items"][number];

function jobColumns(includeCampaign: boolean): ColumnDef<JobRow>[] {
  const campaignColumn: ColumnDef<JobRow> = {
    accessorKey: "campaign_id",
    header: () => <Hint text={hints.jobs.campaign}>Campaign</Hint>,
    cell: ({ getValue }) => (
      <Link
        className="font-mono text-xs text-cyan-300"
        to={`/campaigns/${String(getValue())}`}
      >
        {shortId(String(getValue()))}
      </Link>
    ),
  };
  return [
    {
      accessorKey: "resource_id",
      header: () => <Hint text={hints.jobs.hfJob}>HF Job</Hint>,
      cell: ({ row }) => {
        const resourceId = row.original.resource_id;
        const inspectUrl = row.original.inspect_url;
        if (!resourceId || !inspectUrl)
          return <span className="font-mono text-xs">Pending</span>;
        return (
          <a
            className="inline-flex items-center gap-1 font-mono text-xs text-cyan-300 hover:underline"
            href={inspectUrl}
            rel="noreferrer"
            target="_blank"
          >
            {shortId(resourceId)}
            <ExternalLink size={12} aria-hidden="true" />
            <span className="sr-only">Open Hugging Face Job</span>
          </a>
        );
      },
    },
    ...(includeCampaign ? [campaignColumn] : []),
    {
      accessorKey: "action_kind",
      header: () => <Hint text={hints.jobs.action}>Action</Hint>,
      cell: ({ getValue }) => humanize(String(getValue())),
    },
    {
      accessorKey: "observed_state",
      header: () => <Hint text={hints.jobs.observed}>Observed</Hint>,
      cell: ({ getValue }) => (
        <Badge status={String(getValue() ?? "pending").toLowerCase()}>
          {humanize(String(getValue() ?? "pending"))}
        </Badge>
      ),
    },
    {
      accessorKey: "cost_microusd",
      header: () => <Hint text={hints.jobs.cost}>Cost</Hint>,
      cell: ({ getValue }) => {
        const value = getValue();
        if (typeof value !== "number") throw new Error("Job cost is missing");
        return formatMoney(value);
      },
    },
    {
      accessorKey: "created_at",
      header: () => <Hint text={hints.jobs.recorded}>Recorded</Hint>,
      cell: ({ getValue }) => formatDate(String(getValue())),
    },
  ];
}

function CampaignJobs({ campaignId }: { campaignId: string }) {
  const query = useJobs(undefined, campaignId);
  return (
    <section className="mt-8">
      <h2 className="text-lg font-semibold text-white">
        <Hint text={hints.campaign.jobs}>Jobs</Hint>
      </h2>
      <p className="mb-4 mt-1 text-sm text-slate-400">
        HF Jobs launched for this campaign, with Hub inspect links, latest observed
        state, and recorded hardware cost.
      </p>
      <QueryContent query={query}>
        <DataTable
          columns={jobColumns(false)}
          data={query.data?.items ?? []}
          empty="No Jobs have been launched"
        />
      </QueryContent>
    </section>
  );
}

function useCursorNavigation() {
  const [searchParams, setSearchParams] = useSearchParams();
  const cursor = searchParams.get("cursor") ?? undefined;
  const history = searchParams.getAll("previous_cursor");
  const next = (nextCursor: string) => {
    const updated = new URLSearchParams(searchParams);
    updated.append("previous_cursor", cursor ?? "");
    updated.set("cursor", nextCursor);
    setSearchParams(updated);
  };
  const previous = () => {
    const updated = new URLSearchParams(searchParams);
    const prior = updated.getAll("previous_cursor");
    const previousCursor = prior.at(-1);
    updated.delete("previous_cursor");
    for (const value of prior.slice(0, -1)) updated.append("previous_cursor", value);
    if (previousCursor) updated.set("cursor", previousCursor);
    else updated.delete("cursor");
    setSearchParams(updated);
  };
  const first = () => {
    const updated = new URLSearchParams(searchParams);
    updated.delete("cursor");
    updated.delete("previous_cursor");
    setSearchParams(updated);
  };
  return { cursor, history, next, previous, first };
}

function CursorPager({
  navigation,
  nextCursor,
}: {
  navigation: ReturnType<typeof useCursorNavigation>;
  nextCursor: string | null | undefined;
}) {
  if (!navigation.cursor && !nextCursor) return null;
  return (
    <nav
      aria-label="Collection pages"
      className="mt-4 flex flex-wrap items-center justify-end gap-2"
    >
      <span className="mr-auto text-xs text-slate-500">
        Page {navigation.history.length + 1}
      </span>
      <Button disabled={!navigation.cursor} variant="ghost" onClick={navigation.first}>
        First
      </Button>
      <Button
        disabled={!navigation.cursor}
        variant="secondary"
        onClick={navigation.previous}
      >
        Previous
      </Button>
      <Button
        disabled={!nextCursor}
        onClick={() => nextCursor && navigation.next(nextCursor)}
      >
        Next
      </Button>
    </nav>
  );
}

const launchSchema = z.object({
  benchmark: z.string().min(2),
  model: z.string().min(2),
  harness: z.string().min(2),
  deployment: z.string().min(2).optional(),
  launch_policy: z.string().min(2),
  ceiling_microusd: z.number().int().nonnegative(),
  confirmed: z
    .boolean()
    .refine((value) => value, "Confirm the resolved target and cost ceiling"),
});

function SpendChart({ data }: { data: Array<{ name: string; spend: number }> }) {
  const width = 640;
  const height = 240;
  const padding = 24;
  const maximum = Math.max(...data.map((item) => item.spend), 1);
  const point = (value: number, index: number) => {
    const x =
      data.length === 1
        ? width / 2
        : padding + (index / (data.length - 1)) * (width - padding * 2);
    const y = height - padding - (value / maximum) * (height - padding * 2);
    return [x, y] as const;
  };
  const points = data.map((item, index) => point(item.spend, index));
  const line = points.map(([x, y]) => `${x},${y}`).join(" ");
  const area = `${padding},${height - padding} ${line} ${width - padding},${height - padding}`;
  return (
    <svg
      className="h-full w-full"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="Observed campaign spend trend"
    >
      <title>Observed campaign spend trend</title>
      <defs>
        <linearGradient id="spend-gradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#22d3ee" stopOpacity="0.45" />
          <stop offset="100%" stopColor="#22d3ee" stopOpacity="0.03" />
        </linearGradient>
      </defs>
      {[0.25, 0.5, 0.75, 1].map((ratio) => {
        const y = height - padding - ratio * (height - padding * 2);
        return (
          <line
            key={ratio}
            x1={padding}
            x2={width - padding}
            y1={y}
            y2={y}
            stroke="#1e293b"
            strokeDasharray="4 6"
          />
        );
      })}
      <polygon points={area} fill="url(#spend-gradient)" />
      <polyline
        points={line}
        fill="none"
        stroke="#22d3ee"
        strokeWidth="3"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {points.map(([x, y], index) => (
        <circle key={data[index]?.name} cx={x} cy={y} r="4" fill="#67e8f9">
          <title>{`${data[index]?.name}: ${formatMoney(data[index]?.spend ?? 0)}`}</title>
        </circle>
      ))}
    </svg>
  );
}

function Stat({
  label,
  value,
  note,
  icon: Icon,
  hint,
}: {
  label: string;
  value: string;
  note: string;
  icon: typeof Clock3;
  hint?: string;
}) {
  return (
    <Card>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wider text-slate-500">
            {hint ? <Hint text={hint}>{label}</Hint> : label}
          </p>
          <p className="mt-2 text-2xl font-semibold text-white">{value}</p>
          <p className="mt-1 text-xs text-slate-500">{note}</p>
        </div>
        <span className="rounded-lg bg-cyan-400/10 p-2 text-cyan-300">
          <Icon size={18} />
        </span>
      </div>
    </Card>
  );
}

export function OverviewPage() {
  const campaigns = useCampaigns();
  const endpoints = useEndpoints();
  const system = useSystem();
  const items = campaigns.data?.items ?? [];
  const active = items.filter((item) => item.status !== "completed").length;
  const failures = items.filter((item) =>
    ["failed", "manual_intervention"].includes(item.status),
  ).length;
  const spend = items.reduce((sum, item) => sum + item.observed_microusd, 0);
  const unsafe = (endpoints.data?.items ?? []).filter(
    (item) => !item.cleanup_verified || item.ready_replicas > 0,
  ).length;
  const chart = [...items]
    .reverse()
    .slice(-20)
    .map((item) => ({
      name: shortId(item.campaign_id),
      spend: item.observed_microusd / 1_000_000,
      progress: item.total_tasks
        ? Math.round((item.terminal_tasks / item.total_tasks) * 100)
        : 0,
    }));
  return (
    <QueryContent query={campaigns}>
      <QueryContent query={endpoints}>
        <QueryContent query={system}>
          <PageHeader
            title="Overview"
            description="Campaign progress, spend, publication and endpoint safety from the immutable control record."
          />
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Stat
              label="Active campaigns"
              value={String(active)}
              note={`${items.length} total`}
              icon={PlayCircle}
              hint={hints.overview.active}
            />
            <Stat
              label="Policy stops"
              value={String(failures)}
              note="Requires operator review"
              icon={AlertTriangle}
              hint={hints.overview.policyStops}
            />
            <Stat
              label="Observed spend"
              value={formatMoney(spend)}
              note="Across projected campaigns"
              icon={CircleDollarSign}
              hint={hints.overview.observedSpend}
            />
            <Stat
              label="Unsafe endpoints"
              value={String(unsafe)}
              note={unsafe ? "Cleanup required" : "All observed endpoints safe"}
              icon={ShieldCheck}
              hint={hints.overview.unsafeEndpoints}
            />
          </div>
          <div className="mt-6 grid gap-6 xl:grid-cols-[1.4fr_1fr]">
            <Card>
              <h2 className="font-semibold">
                <Hint text={hints.overview.spendChart}>Recent campaign spend</Hint>
              </h2>
              <p className="mt-1 text-xs text-slate-500">
                Observed cost in USD; reserved ceilings remain separate.
              </p>
              <div className="mt-6 h-72">
                {chart.length ? (
                  <SpendChart data={chart} />
                ) : (
                  <div className="grid h-full place-items-center text-sm text-slate-500">
                    No campaign spend yet
                  </div>
                )}
              </div>
            </Card>
            <Card>
              <div className="flex items-center justify-between">
                <h2 className="font-semibold">Control readiness</h2>
                <Badge status={system.data?.projection?.ready ? "ready" : "pending"}>
                  {system.data?.projection?.ready ? "Ready" : "Rebuilding"}
                </Badge>
              </div>
              <dl className="mt-5 space-y-4 text-sm">
                <div>
                  <div className="flex justify-between gap-4">
                    <dt className="text-slate-500">
                      <Hint text={hints.overview.writeMode}>Write mode</Hint>
                    </dt>
                    <dd>{humanize(String(system.data?.write_mode ?? "unknown"))}</dd>
                  </div>
                  <p className="mt-1 text-xs leading-5 text-slate-500">
                    Write mode is a deployment safety switch. User roles are checked
                    separately.
                  </p>
                </div>
                <div>
                  <dt className="text-slate-500">
                    <Hint text={hints.overview.sourceRevision}>Source revision</Hint>
                  </dt>
                  <dd className="mt-1 break-all font-mono text-xs select-all">
                    {String(system.data?.source_revision ?? "unknown")}
                  </dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-slate-500">
                    <Hint text={hints.overview.projectedObjects}>
                      Projected objects
                    </Hint>
                  </dt>
                  <dd>{String(system.data?.projection?.object_count ?? 0)}</dd>
                </div>
                <div>
                  <div className="flex justify-between gap-4">
                    <dt className="text-slate-500">
                      <Hint text={hints.overview.projectionFreshness}>
                        Projection freshness
                      </Hint>
                    </dt>
                    <dd>
                      {system.data?.projection?.last_sync_at
                        ? formatDate(system.data.projection.last_sync_at)
                        : "No successful sync"}
                    </dd>
                  </div>
                  <p className="mt-1 text-xs leading-5 text-slate-500">
                    The projection is a disposable read view rebuilt from immutable
                    records.
                  </p>
                </div>
              </dl>
            </Card>
          </div>
        </QueryContent>
      </QueryContent>
    </QueryContent>
  );
}

function LaunchPanel({ onClose }: { onClose(): void }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const profiles = useProfiles();
  const { writesAllowed, writeMode } = useControlState();
  const form = useForm<z.infer<typeof launchSchema>>({
    resolver: zodResolver(launchSchema),
    defaultValues: {
      benchmark: "control-smoke",
      model: "control-smoke",
      harness: "control-smoke",
      deployment: "hf-cpu-smoke",
      launch_policy: "control-smoke",
      ceiling_microusd: 0,
      confirmed: false,
    },
  });
  const values = form.watch();
  const approved = (profiles.data?.items ?? []).flatMap((profile) =>
    profile.approved_aliases.map((approved_alias) => ({
      ...profile,
      approved_alias,
    })),
  );
  const options = (kind: string) =>
    approved.filter((profile) => {
      if (profile.profile_kind !== kind) return false;
      if (kind !== "deployment") return true;
      const spec = profile.spec as Record<string, unknown>;
      return (
        Array.isArray(spec.models) &&
        spec.models.includes(values.model) &&
        Array.isArray(spec.harnesses) &&
        spec.harnesses.includes(values.harness)
      );
    });
  const selectedAlias = (kind: string): string | undefined => {
    if (kind === "benchmark") return values.benchmark;
    if (kind === "model") return values.model;
    if (kind === "harness") return values.harness;
    if (kind === "deployment") return values.deployment;
    if (kind === "launch_policy") return values.launch_policy;
    return undefined;
  };
  const selected = approved.filter(
    (profile) => profile.approved_alias === selectedAlias(profile.profile_kind),
  );
  const selectedByKind = (kind: string) =>
    selected.find((profile) => profile.profile_kind === kind);
  const benchmarkSpec = selectedByKind("benchmark")?.spec as
    | Record<string, unknown>
    | undefined;
  const modelSpec = selectedByKind("model")?.spec as
    | Record<string, unknown>
    | undefined;
  const deploymentSpec = selectedByKind("deployment")?.spec as
    | Record<string, unknown>
    | undefined;
  const policySpec = selectedByKind("launch_policy")?.spec as
    | Record<string, unknown>
    | undefined;
  const taskCount = Array.isArray(benchmarkSpec?.task_ids)
    ? benchmarkSpec.task_ids.length
    : 0;
  const attemptLimit = Number(policySpec?.max_infrastructure_attempts ?? 0);
  const estimatedMicrousd = estimateLaunchReservationMicrousd(
    taskCount,
    deploymentSpec,
    policySpec,
  );
  const mutation = useMutation({
    mutationFn: (input: CampaignSubmission) => submitCampaign(input),
    onSuccess: async (result) => {
      await client.invalidateQueries({ queryKey: keys.campaigns });
      navigate(`/campaigns/${result.campaign_id}`);
    },
  });
  return (
    <Card className="mb-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-semibold">Launch campaign</h2>
          <p className="mt-1 text-sm text-slate-400">
            Hover a field name for what it locks. Select approved aliases and review
            their immutable resolution before any paid action.
          </p>
        </div>
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      {!writesAllowed ? (
        <p className="mt-4 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm text-amber-200">
          Launch is unavailable because write mode is {humanize(writeMode)}. Your role
          and deployment write mode are separate controls.
        </p>
      ) : null}
      <QueryContent query={profiles}>
        <form
          className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-3"
          onSubmit={form.handleSubmit((value) =>
            mutation.mutate(value as CampaignSubmission),
          )}
        >
          {(
            ["benchmark", "model", "harness", "deployment", "launch_policy"] as const
          ).map((field) => (
            <div className="space-y-1.5 text-sm" key={field}>
              <Hint text={hints.launch[field]} icon>
                {humanize(field)}
              </Hint>
              <select
                className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2 outline-none focus:ring-2 focus:ring-cyan-400 disabled:opacity-50"
                disabled={!writesAllowed}
                aria-label={humanize(field)}
                {...form.register(field)}
              >
                {options(field).map((profile) => (
                  <option key={profile.profile_id} value={profile.approved_alias ?? ""}>
                    {profile.approved_alias}
                  </option>
                ))}
              </select>
            </div>
          ))}
          <div className="space-y-1.5 text-sm">
            <Hint text={hints.launch.ceiling} icon>
              Hard cost ceiling, USD
            </Hint>
            <input
              className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2 outline-none focus:ring-2 focus:ring-cyan-400 disabled:opacity-50"
              type="number"
              min="0"
              step="0.000001"
              disabled={!writesAllowed}
              aria-label="Hard cost ceiling, USD"
              {...form.register("ceiling_microusd", {
                setValueAs: (value) => Math.round(Number(value) * 1_000_000),
              })}
            />
          </div>
          <Card className="md:col-span-2 xl:col-span-3">
            <h3 className="font-medium">Resolved launch</h3>
            <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.launch.logicalTasks}>Logical tasks</Hint>
                </dt>
                <dd>{taskCount}</dd>
              </div>
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.launch.modelRevision}>Model revision</Hint>
                </dt>
                <dd className="break-all font-mono text-xs">
                  {String(modelSpec?.revision ?? "Unavailable")}
                </dd>
              </div>
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.launch.hardware}>Hardware</Hint>
                </dt>
                <dd>{String(deploymentSpec?.hardware ?? "Unavailable")}</dd>
              </div>
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.launch.attemptLimit}>Attempt limit</Hint>
                </dt>
                <dd>{attemptLimit || "Unavailable"}</dd>
              </div>
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.launch.estimatedReservation}>
                    Estimated reservation
                  </Hint>
                </dt>
                <dd>{formatMoney(estimatedMicrousd)}</dd>
              </div>
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.launch.hardCeiling}>Hard ceiling</Hint>
                </dt>
                <dd>
                  {formatMoney(values.ceiling_microusd || 0)}{" "}
                  <span className="text-xs text-slate-500">
                    ({values.ceiling_microusd || 0} microusd)
                  </span>
                </dd>
              </div>
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.launch.publicationRole}>Publication role</Hint>
                </dt>
                <dd>
                  {humanize(String(policySpec?.publication_role ?? "Unavailable"))}
                </dd>
              </div>
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.launch.perJobReservation}>Per-Job reservation</Hint>
                </dt>
                <dd>{formatMoney(Number(policySpec?.reservation_microusd ?? 0))}</dd>
              </div>
            </dl>
            <div className="mt-4 grid gap-3 lg:grid-cols-2">
              {selected.map((profile) => (
                <details
                  className="rounded-md border border-slate-800 p-3"
                  key={`${profile.profile_kind}:${profile.profile_id}`}
                >
                  <summary className="cursor-pointer text-sm font-medium">
                    {humanize(profile.profile_kind)}: {profile.approved_alias}
                  </summary>
                  <p className="mt-2 break-all font-mono text-xs text-slate-500">
                    {profile.profile_id}
                  </p>
                  <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap text-xs text-slate-400">
                    {JSON.stringify(profile.spec, null, 2)}
                  </pre>
                </details>
              ))}
            </div>
          </Card>
          <div className="flex flex-wrap items-start gap-3 md:col-span-2 xl:col-span-3">
            <label className="flex items-start gap-3">
              <input
                className="mt-1 h-4 w-4 accent-cyan-400"
                type="checkbox"
                disabled={!writesAllowed}
                {...form.register("confirmed")}
              />
              <span className="text-sm text-slate-300">
                I confirm the resolved profiles, logical task count, estimated
                reservation, and hard cost ceiling.
              </span>
            </label>
            <Hint text={hints.launch.confirmed} icon>
              Why this is required
            </Hint>
          </div>
          {form.formState.errors.confirmed ? (
            <p className="text-sm text-rose-300 md:col-span-2 xl:col-span-3">
              {form.formState.errors.confirmed.message}
            </p>
          ) : null}
          {mutation.error ? (
            <p className="text-sm text-rose-300 md:col-span-2 xl:col-span-3">
              {mutation.error.message}
            </p>
          ) : null}
          <div className="md:col-span-2 xl:col-span-3">
            <Button disabled={!writesAllowed || mutation.isPending} type="submit">
              <PlayCircle size={16} />
              {mutation.isPending ? "Submitting" : "Create immutable campaign"}
            </Button>
          </div>
        </form>
      </QueryContent>
    </Card>
  );
}

export function CampaignsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigation = useCursorNavigation();
  const [launching, setLaunching] = useState(false);
  const query = useCampaigns(navigation.cursor);
  const { writesAllowed, writeMode } = useControlState();
  const filter = searchParams.get("status") ?? "all";
  const items = (query.data?.items ?? []).filter(
    (item) => filter === "all" || item.status === filter,
  );
  const columns = useMemo<ColumnDef<CampaignRow>[]>(
    () => [
      {
        accessorKey: "campaign_id",
        header: () => <Hint text={hints.campaign.identity}>Campaign</Hint>,
        cell: ({ row }) => (
          <Link
            className="font-mono text-xs text-cyan-300 hover:underline"
            to={`/campaigns/${row.original.campaign_id}`}
          >
            {shortId(row.original.campaign_id)}
          </Link>
        ),
      },
      {
        accessorKey: "status",
        header: () => <Hint text={hints.campaign.status}>State</Hint>,
        cell: ({ getValue }) => (
          <Badge status={String(getValue())}>{humanize(String(getValue()))}</Badge>
        ),
      },
      {
        id: "progress",
        header: () => <Hint text={hints.campaign.logicalTasks}>Logical progress</Hint>,
        cell: ({ row }) => (
          <div className="min-w-40">
            <Progress
              label={`${row.original.terminal_tasks}/${row.original.total_tasks} tasks`}
              value={
                row.original.total_tasks
                  ? (row.original.terminal_tasks / row.original.total_tasks) * 100
                  : 0
              }
            />
          </div>
        ),
      },
      {
        accessorKey: "observed_microusd",
        header: () => <Hint text={hints.campaign.observedCost}>Observed</Hint>,
        cell: ({ getValue }) => formatMoney(Number(getValue())),
      },
      {
        accessorKey: "ceiling_microusd",
        header: () => <Hint text={hints.launch.hardCeiling}>Ceiling</Hint>,
        cell: ({ getValue }) => formatMoney(Number(getValue())),
      },
      {
        accessorKey: "created_at",
        header: () => <Hint text={hints.audit.time}>Created</Hint>,
        cell: ({ getValue }) => formatDate(String(getValue())),
      },
    ],
    [],
  );
  return (
    <>
      <PageHeader
        title="Campaigns"
        description="Logical tasks remain separate from physical attempts and infrastructure repairs."
        action={
          <Button
            disabled={!writesAllowed}
            title={
              writesAllowed
                ? "Launch a campaign"
                : `Launch is unavailable while write mode is ${writeMode}`
            }
            onClick={() => setLaunching((value) => !value)}
          >
            <Plus size={16} />
            Launch
          </Button>
        }
      />
      {launching ? <LaunchPanel onClose={() => setLaunching(false)} /> : null}
      <nav className="mb-4 flex gap-2" aria-label="Filter campaigns">
        {["all", "active", "publishing", "completed"].map((status) => (
          <Button
            key={status}
            variant={filter === status ? "secondary" : "ghost"}
            onClick={() => setSearchParams(status === "all" ? {} : { status })}
          >
            {humanize(status)}
          </Button>
        ))}
      </nav>
      <QueryContent query={query}>
        <DataTable
          columns={columns}
          data={items}
          empty="No campaigns match this filter"
        />
        <CursorPager navigation={navigation} nextCursor={query.data?.next_cursor} />
      </QueryContent>
    </>
  );
}

export function CampaignPage() {
  const { campaignId = "" } = useParams();
  const navigation = useCursorNavigation();
  const campaign = useCampaign(campaignId);
  const tasks = useTasks(campaignId, navigation.cursor);
  const client = useQueryClient();
  const { writesAllowed, writeMode } = useControlState();
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelAcknowledged, setCancelAcknowledged] = useState(false);
  const closeCancel = () => {
    setCancelOpen(false);
    setCancelAcknowledged(false);
  };
  const cancel = useMutation({
    mutationFn: () =>
      actOnCampaign(campaignId, {
        action: "cancel",
        task_id: null,
        reason: "operator cancellation",
        confirmed: true,
      } as CampaignAction),
    onSuccess: () => {
      closeCancel();
      return client.invalidateQueries({ queryKey: keys.campaign(campaignId) });
    },
  });
  if (!campaign.data)
    return (
      <QueryContent query={campaign}>
        <Empty>Campaign not found</Empty>
      </QueryContent>
    );
  const item = campaign.data;
  const columns: ColumnDef<TaskRow>[] = [
    {
      accessorKey: "task_id",
      header: () => <Hint text={hints.campaign.logicalTasks}>Task</Hint>,
      cell: ({ row }) => (
        <Link
          className="font-mono text-xs text-cyan-300 hover:underline"
          to={`/campaigns/${campaignId}/tasks/${row.original.task_id}`}
        >
          {shortId(row.original.task_id)}
        </Link>
      ),
    },
    {
      accessorKey: "terminal_outcome",
      header: () => <Hint text={hints.campaign.outcome}>Outcome</Hint>,
      cell: ({ getValue }) => (
        <Badge status={String(getValue() ?? "pending")}>
          {humanize(String(getValue() ?? "pending"))}
        </Badge>
      ),
    },
    {
      accessorKey: "selected_attempt_id",
      header: () => <Hint text={hints.campaign.selectedAttempt}>Selected attempt</Hint>,
      cell: ({ getValue }) => (
        <span className="font-mono text-xs">
          {getValue() ? shortId(String(getValue())) : "—"}
        </span>
      ),
    },
    {
      accessorKey: "input_digest",
      header: () => <Hint text={hints.campaign.inputDigest}>Input</Hint>,
      cell: ({ getValue }) => (
        <span className="font-mono text-xs text-slate-500">
          {shortId(String(getValue()))}
        </span>
      ),
    },
  ];
  return (
    <QueryContent query={campaign}>
      <PageHeader
        title={shortId(campaignId)}
        description="Campaign lock, logical outcomes, cost and publication state."
        action={
          item.status !== "completed" ? (
            <Button
              variant="destructive"
              disabled={!writesAllowed || cancel.isPending}
              title={
                writesAllowed
                  ? "Cancel this campaign"
                  : `Cancellation is unavailable while write mode is ${writeMode}`
              }
              onClick={() => setCancelOpen(true)}
            >
              <PauseCircle size={16} />
              Cancel campaign
            </Button>
          ) : undefined
        }
      />
      {cancelOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4">
          <Card
            className="w-full max-w-lg border-rose-500/40"
            role="dialog"
            aria-modal="true"
            aria-labelledby="cancel-campaign-title"
            aria-describedby="cancel-campaign-effect"
          >
            <h2 id="cancel-campaign-title" className="text-lg font-semibold text-white">
              Cancel campaign?
            </h2>
            <p className="mt-2 text-sm text-slate-300">
              Target <span className="font-mono">{shortId(campaignId)}</span> has{" "}
              {item.total_tasks - item.terminal_tasks} open logical tasks.
            </p>
            <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
              <div>
                <dt className="text-slate-500">Observed</dt>
                <dd>{formatMoney(item.observed_microusd)}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Reserved</dt>
                <dd>{formatMoney(item.reserved_microusd)}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Ceiling</dt>
                <dd>{formatMoney(item.ceiling_microusd)}</dd>
              </div>
            </dl>
            <p id="cancel-campaign-effect" className="mt-4 text-sm text-slate-300">
              This stops or observes active remote Jobs, prevents queued launches, and
              seals open tasks as cancelled. Evidence is retained, and publication still
              waits for endpoint cleanup.
            </p>
            <label className="mt-4 flex items-start gap-3 text-sm text-slate-200">
              <input
                className="mt-1 h-4 w-4 accent-rose-500"
                type="checkbox"
                checked={cancelAcknowledged}
                onChange={(event) => setCancelAcknowledged(event.target.checked)}
              />
              I understand this cancellation cannot reopen sealed logical tasks.
            </label>
            <div className="mt-6 flex justify-end gap-2">
              <Button variant="ghost" onClick={closeCancel} disabled={cancel.isPending}>
                Keep running
              </Button>
              <Button
                variant="destructive"
                disabled={!writesAllowed || !cancelAcknowledged || cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                {cancel.isPending ? "Cancelling…" : "Confirm cancellation"}
              </Button>
            </div>
          </Card>
        </div>
      ) : null}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Status"
          value={humanize(item.status)}
          note={item.publication_status ?? "Not published"}
          icon={RefreshCw}
          hint={hints.campaign.status}
        />
        <Stat
          label="Logical tasks"
          value={`${item.terminal_tasks}/${item.total_tasks}`}
          note={`${item.pending_actions} pending actions`}
          icon={Clock3}
          hint={hints.campaign.logicalTasks}
        />
        <Stat
          label="Observed cost"
          value={formatMoney(item.observed_microusd)}
          note={`All recorded campaign sources. ${formatMoney(item.reserved_microusd)} reserved`}
          icon={CircleDollarSign}
          hint={hints.campaign.observedCost}
        />
        <Stat
          label="Endpoint cleanup"
          value={item.cleanup_pending ? "Pending" : "Clear"}
          note="Required before completion"
          icon={ShieldCheck}
          hint={hints.campaign.endpointCleanup}
        />
      </div>
      <Card className="my-6">
        <Progress
          label="Terminal logical outcomes"
          value={item.total_tasks ? (item.terminal_tasks / item.total_tasks) * 100 : 0}
        />
      </Card>
      <QueryContent query={tasks}>
        <DataTable
          columns={columns}
          data={tasks.data?.items ?? []}
          empty="No tasks are locked"
        />
        <CursorPager navigation={navigation} nextCursor={tasks.data?.next_cursor} />
      </QueryContent>
      <CampaignJobs campaignId={campaignId} />
    </QueryContent>
  );
}

export function TaskPage() {
  const { campaignId = "", taskId = "" } = useParams();
  const detail = useTask(campaignId, taskId);
  if (!detail.data)
    return (
      <QueryContent query={detail}>
        <Empty>Task not found</Empty>
      </QueryContent>
    );
  return (
    <QueryContent query={detail}>
      <PageHeader
        title={shortId(taskId)}
        description="One logical task with every immutable physical attempt."
      />
      <Card>
        <dl className="grid gap-4 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-slate-500">
              <Hint text={hints.campaign.outcome}>Outcome</Hint>
            </dt>
            <dd className="mt-1">
              <Badge status={detail.data.task.terminal_outcome ?? "pending"}>
                {humanize(detail.data.task.terminal_outcome ?? "pending")}
              </Badge>
            </dd>
          </div>
          <div>
            <dt className="text-slate-500">
              <Hint text={hints.campaign.selectedAttempt}>Selected attempt</Hint>
            </dt>
            <dd className="mt-1 font-mono text-xs">
              {detail.data.task.selected_attempt_id ?? "—"}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-slate-500">
              <Hint text={hints.campaign.inputDigest}>Input digest</Hint>
            </dt>
            <dd className="mt-1 break-all font-mono text-xs">
              {detail.data.task.input_digest}
            </dd>
          </div>
        </dl>
      </Card>
      <div className="mt-6 space-y-4">
        {detail.data.attempts.map((attempt, index) => (
          <Card key={attempt.attempt_id}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-wider text-slate-500">
                  Physical attempt {index + 1}
                </p>
                <p className="mt-1 font-mono text-sm">{attempt.attempt_id}</p>
              </div>
              <Badge status={attempt.outcome}>{humanize(attempt.outcome)}</Badge>
            </div>
            <dl className="mt-5 grid gap-4 text-sm sm:grid-cols-3">
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.campaign.replacementEligible}>
                    Replacement eligible
                  </Hint>
                </dt>
                <dd>{attempt.replacement_eligible ? "Yes" : "No"}</dd>
              </div>
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.campaign.attemptCost}>Cost</Hint>
                </dt>
                <dd>{formatMoney(attempt.cost_microusd)}</dd>
              </div>
              <div>
                <dt className="text-slate-500">
                  <Hint text={hints.campaign.attemptRecorded}>Recorded</Hint>
                </dt>
                <dd>{formatDate(attempt.created_at)}</dd>
              </div>
              <div className="sm:col-span-3">
                <dt className="text-slate-500">
                  <Hint text={hints.campaign.attemptMetrics}>Metrics</Hint>
                </dt>
                <dd className="mt-1 flex flex-wrap gap-2">
                  {Object.entries(attempt.metrics).length ? (
                    Object.entries(attempt.metrics).map(([name, value]) => (
                      <Badge key={name}>{`${name}: ${value}`}</Badge>
                    ))
                  ) : (
                    <span className="text-slate-500">No metrics</span>
                  )}
                </dd>
              </div>
            </dl>
          </Card>
        ))}
      </div>
      <CampaignJobs campaignId={campaignId} />
    </QueryContent>
  );
}

export function JobsPage() {
  const navigation = useCursorNavigation();
  const query = useJobs(navigation.cursor);
  return (
    <>
      <PageHeader
        title="Jobs"
        description="Current HF Job identity, ownership, and latest observed infrastructure state."
      />
      <QueryContent query={query}>
        <DataTable columns={jobColumns(true)} data={query.data?.items ?? []} />
        <CursorPager navigation={navigation} nextCursor={query.data?.next_cursor} />
      </QueryContent>
    </>
  );
}

export function EndpointsPage() {
  const navigation = useCursorNavigation();
  const query = useEndpoints(navigation.cursor);
  const columns: ColumnDef<EndpointRow>[] = [
    {
      accessorKey: "endpoint_id",
      header: () => <Hint text={hints.endpoints.endpoint}>Endpoint</Hint>,
      cell: ({ getValue }) => (
        <span className="font-mono text-xs">{shortId(String(getValue()))}</span>
      ),
    },
    {
      accessorKey: "campaign_id",
      header: () => <Hint text={hints.endpoints.campaign}>Campaign</Hint>,
      cell: ({ getValue }) => (
        <Link
          className="font-mono text-xs text-cyan-300"
          to={`/campaigns/${String(getValue())}`}
        >
          {shortId(String(getValue()))}
        </Link>
      ),
    },
    {
      accessorKey: "desired_state",
      header: () => <Hint text={hints.endpoints.desired}>Desired</Hint>,
      cell: ({ getValue }) => humanize(String(getValue())),
    },
    {
      accessorKey: "observed_state",
      header: () => <Hint text={hints.endpoints.observed}>Observed</Hint>,
      cell: ({ getValue }) => (
        <Badge status={String(getValue()).toLowerCase()}>
          {humanize(String(getValue()))}
        </Badge>
      ),
    },
    {
      accessorKey: "ready_replicas",
      header: () => <Hint text={hints.endpoints.readyReplicas}>Ready replicas</Hint>,
    },
    {
      accessorKey: "cleanup_verified",
      header: () => <Hint text={hints.endpoints.cleanup}>Cleanup</Hint>,
      cell: ({ getValue }) =>
        Number(getValue()) ? (
          <Badge status="ready">Verified</Badge>
        ) : (
          <Badge status="pending">Pending</Badge>
        ),
    },
    {
      accessorKey: "active_hourly_cost_microusd",
      header: () => <Hint text={hints.endpoints.hourly}>Active hourly cost</Hint>,
      cell: ({ getValue }) => formatMoney(Number(getValue())),
    },
  ];
  return (
    <>
      <PageHeader
        title="Endpoints"
        description="Requested and observed state remain separate. Completion requires verified pause with zero ready replicas."
      />
      <QueryContent query={query}>
        <DataTable columns={columns} data={query.data?.items ?? []} />
        <CursorPager navigation={navigation} nextCursor={query.data?.next_cursor} />
      </QueryContent>
    </>
  );
}

export function ResultsPage() {
  const navigation = useCursorNavigation();
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = {
    model: searchParams.get("model") ?? undefined,
    benchmark: searchParams.get("benchmark") ?? undefined,
    agent: searchParams.get("agent") ?? undefined,
    status: searchParams.get("result_status") ?? undefined,
    search: searchParams.get("search") ?? undefined,
    published_after: searchParams.get("published_after") ?? undefined,
    published_before: searchParams.get("published_before") ?? undefined,
    sort: (searchParams.get("sort") ?? "published_at") as
      | "published_at"
      | "model"
      | "benchmark"
      | "status"
      | "score",
    order: (searchParams.get("order") ?? "desc") as "asc" | "desc",
  };
  const query = useResults(navigation.cursor, filters);
  const columns: ColumnDef<ResultRow>[] = [
    {
      accessorKey: "publication_id",
      header: () => <Hint text={hints.results.publication}>Publication</Hint>,
      cell: ({ row }) => (
        <Link
          className="font-mono text-xs text-cyan-300 hover:underline"
          to={`/results/${row.original.publication_id}`}
        >
          {shortId(row.original.publication_id)}
        </Link>
      ),
    },
    {
      accessorKey: "model",
      header: () => (
        <Hint text={hints.results.modelBenchmark}>Model and benchmark</Hint>
      ),
      cell: ({ row }) => (
        <div>
          <div className="max-w-52 truncate font-medium">
            {String(row.original.model ?? "—")}
          </div>
          <div className="max-w-52 truncate text-xs text-slate-500">
            {String(row.original.benchmark ?? "")}
          </div>
        </div>
      ),
    },
    {
      accessorKey: "agent",
      header: () => <Hint text={hints.results.agent}>Agent</Hint>,
      cell: ({ row }) => String(row.original.agent ?? row.original.harness ?? "—"),
    },
    {
      id: "score",
      header: () => <Hint text={hints.results.primaryMetric}>Primary metric</Hint>,
      cell: ({ row }) => {
        const metric = row.original.primary_metric;
        return metric ? `${metric.value.toFixed(4)} ${metric.unit}` : "—";
      },
    },
    {
      id: "tasks",
      header: () => <Hint text={hints.results.scoredTasks}>Scored tasks</Hint>,
      cell: ({ row }) =>
        row.original.task_count === null || row.original.task_count === undefined
          ? "—"
          : `${row.original.scored_task_count ?? 0}/${row.original.task_count}`,
    },
    {
      accessorKey: "status",
      header: () => <Hint text={hints.results.state}>State</Hint>,
      cell: ({ getValue }) => (
        <Badge status={String(getValue())}>{humanize(String(getValue()))}</Badge>
      ),
    },
    {
      accessorKey: "published_at",
      header: () => <Hint text={hints.results.published}>Published</Hint>,
      cell: ({ getValue }) => formatDate(String(getValue())),
    },
  ];
  return (
    <>
      <PageHeader
        title="Results"
        description="Search normalized results and open stable detail views with allowlisted provenance."
      />
      <form
        className="mb-5 grid gap-3 rounded-xl border border-slate-800 bg-slate-950/70 p-4 sm:grid-cols-2 xl:grid-cols-4"
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          const next = new URLSearchParams();
          for (const key of [
            "search",
            "model",
            "benchmark",
            "agent",
            "result_status",
            "sort",
            "order",
          ] as const) {
            const value = String(data.get(key) ?? "").trim();
            if (
              value &&
              !(
                (key === "sort" && value === "published_at") ||
                (key === "order" && value === "desc")
              )
            )
              next.set(key, value);
          }
          const after = String(data.get("published_after") ?? "");
          const before = String(data.get("published_before") ?? "");
          if (after) next.set("published_after", `${after}T00:00:00.000Z`);
          if (before) next.set("published_before", `${before}T23:59:59.999Z`);
          setSearchParams(next);
        }}
      >
        {(["search", "model", "benchmark", "agent"] as const).map((name) => (
          <label className="space-y-1 text-sm" key={name}>
            <span className="text-slate-400">
              <Hint text={hints.results[name]}>{humanize(name)}</Hint>
            </span>
            <input
              className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
              name={name}
              defaultValue={searchParams.get(name) ?? ""}
              type={name === "search" ? "search" : "text"}
            />
          </label>
        ))}
        <label className="space-y-1 text-sm">
          <span className="text-slate-400">
            <Hint text={hints.results.status}>Status</Hint>
          </span>
          <select
            className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
            name="result_status"
            defaultValue={filters.status ?? ""}
          >
            <option value="">All</option>
            <option value="published">Published</option>
            <option value="pending">Pending</option>
            <option value="failed">Failed</option>
          </select>
        </label>
        <label className="space-y-1 text-sm">
          <span className="text-slate-400">
            <Hint text={hints.results.fromDate}>From date</Hint>
          </span>
          <input
            className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
            name="published_after"
            type="date"
            defaultValue={filters.published_after?.slice(0, 10) ?? ""}
          />
        </label>
        <label className="space-y-1 text-sm">
          <span className="text-slate-400">
            <Hint text={hints.results.throughDate}>Through date</Hint>
          </span>
          <input
            className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
            name="published_before"
            type="date"
            defaultValue={filters.published_before?.slice(0, 10) ?? ""}
          />
        </label>
        <div className="grid grid-cols-2 gap-2">
          <label className="space-y-1 text-sm">
            <span className="text-slate-400">
              <Hint text={hints.results.sort}>Sort</Hint>
            </span>
            <select
              className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
              name="sort"
              defaultValue={filters.sort}
            >
              <option value="published_at">Published</option>
              <option value="score">Score</option>
              <option value="model">Model</option>
              <option value="benchmark">Benchmark</option>
              <option value="status">Status</option>
            </select>
          </label>
          <label className="space-y-1 text-sm">
            <span className="text-slate-400">
              <Hint text={hints.results.order}>Order</Hint>
            </span>
            <select
              className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
              name="order"
              defaultValue={filters.order}
            >
              <option value="desc">Descending</option>
              <option value="asc">Ascending</option>
            </select>
          </label>
        </div>
        <div className="flex items-end gap-2 sm:col-span-2 xl:col-span-4">
          <Button type="submit">Apply filters</Button>
          <Button type="button" variant="ghost" onClick={() => setSearchParams({})}>
            Clear
          </Button>
        </div>
      </form>
      <QueryContent query={query}>
        <DataTable
          columns={columns}
          data={query.data?.items ?? []}
          empty="No results match these filters"
        />
        <CursorPager navigation={navigation} nextCursor={query.data?.next_cursor} />
      </QueryContent>
    </>
  );
}

function ResultField({
  label,
  value,
  hint,
}: {
  label: string;
  value: unknown;
  hint?: string;
}) {
  return (
    <div>
      <dt className="text-slate-500">
        {hint ? <Hint text={hint}>{label}</Hint> : label}
      </dt>
      <dd className="mt-1 break-all">
        {value === null || value === undefined || value === "" ? "—" : String(value)}
      </dd>
    </div>
  );
}

export function ResultPage() {
  const { publicationId = "" } = useParams();
  const result = useResult(publicationId);
  if (!result.data)
    return (
      <QueryContent query={result}>
        <Empty>Result not found</Empty>
      </QueryContent>
    );
  const item: ResultDetail = result.data;
  return (
    <QueryContent query={result}>
      <PageHeader
        title={shortId(item.publication_id)}
        description="Result scores, revisions, campaign identity, and browser-safe provenance."
      />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Status"
          value={humanize(item.status)}
          note={item.quality ? humanize(item.quality) : "No quality label"}
          icon={RefreshCw}
          hint={hints.results.state}
        />
        <Stat
          label="Primary metric"
          value={item.primary_metric ? item.primary_metric.value.toFixed(4) : "—"}
          note={
            item.primary_metric
              ? `${item.primary_metric.name} (${item.primary_metric.unit})`
              : "No score"
          }
          icon={Gauge}
          hint={hints.results.primaryMetric}
        />
        <Stat
          label="Scored tasks"
          value={`${item.scored_task_count ?? 0}/${item.task_count ?? 0}`}
          note={`${item.strict_pass_count ?? 0} strict passes`}
          icon={ShieldCheck}
          hint={hints.results.scoredTasks}
        />
        <Stat
          label="Published"
          value={formatDate(item.published_at)}
          note={item.publication_role ? humanize(item.publication_role) : "No role"}
          icon={Clock3}
          hint={hints.results.published}
        />
      </div>
      <Card className="mt-6">
        <h2 className="font-semibold">Result</h2>
        <dl className="mt-4 grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
          <ResultField label="Model" value={item.model} />
          <ResultField label="Benchmark" value={item.benchmark} />
          <ResultField label="Agent" value={item.agent ?? item.harness} />
          <ResultField label="Outcome" value={item.run_outcome} />
          <ResultField label="Model revision" value={item.model_revision} />
          <ResultField label="Benchmark revision" value={item.benchmark_revision} />
          <ResultField label="Harness revision" value={item.harness_revision} />
          <div>
            <dt className="text-slate-500">Campaign</dt>
            <dd className="mt-1">
              <Link
                className="break-all font-mono text-xs text-cyan-300 hover:underline"
                to={`/campaigns/${item.campaign_id}`}
              >
                {item.campaign_id}
              </Link>
            </dd>
          </div>
        </dl>
      </Card>
      <Card className="mt-6">
        <h2 className="font-semibold">Allowlisted provenance</h2>
        <dl className="mt-4 grid gap-4 text-sm sm:grid-cols-2">
          <ResultField label="Publication ID" value={item.publication_id} />
          <ResultField label="Run ID" value={item.run_id} />
          <ResultField label="Catalog digest" value={item.catalog_digest} />
          <ResultField
            label="Catalog source digest"
            value={item.catalog_source_digest}
          />
          <ResultField label="Profile source revision" value={item.source_revision} />
          <ResultField label="Result record path" value={item.result_path} />
          {Object.entries(item.profile_ids ?? {}).map(([kind, id]) => (
            <ResultField key={kind} label={`${humanize(kind)} profile ID`} value={id} />
          ))}
        </dl>
      </Card>
    </QueryContent>
  );
}

export function ProfilesPage() {
  const navigation = useCursorNavigation();
  const query = useProfiles(navigation.cursor);
  const columns: ColumnDef<ProfileRow>[] = [
    {
      accessorKey: "name",
      header: () => <Hint text={hints.profiles.name}>Name</Hint>,
      cell: ({ row }) => (
        <div>
          <div className="font-medium">{row.original.name}</div>
          <div className="font-mono text-xs text-slate-500">
            {shortId(row.original.profile_id)}
          </div>
        </div>
      ),
    },
    {
      accessorKey: "profile_kind",
      header: () => <Hint text={hints.profiles.kind}>Kind</Hint>,
      cell: ({ getValue }) => humanize(String(getValue())),
    },
    {
      accessorKey: "source",
      header: () => <Hint text={hints.profiles.source}>Source</Hint>,
      cell: ({ getValue }) => <Badge>{humanize(String(getValue()))}</Badge>,
    },
    {
      accessorKey: "promotion_state",
      header: () => <Hint text={hints.profiles.approval}>Approval</Hint>,
      cell: ({ getValue }) => (
        <Badge status={String(getValue() ?? "built-in")}>
          {humanize(String(getValue() ?? "built-in"))}
        </Badge>
      ),
    },
    {
      accessorKey: "approved_aliases",
      header: () => <Hint text={hints.profiles.aliases}>Approved aliases</Hint>,
      cell: ({ row }) => row.original.approved_aliases.join(", ") || "—",
    },
  ];
  return (
    <>
      <PageHeader
        title="Profiles"
        description="Resolved, immutable benchmark, model, harness, deployment and launch-policy records."
      />
      <QueryContent query={query}>
        <DataTable columns={columns} data={query.data?.items ?? []} />
        <CursorPager navigation={navigation} nextCursor={query.data?.next_cursor} />
      </QueryContent>
    </>
  );
}

export function AuditPage() {
  const navigation = useCursorNavigation();
  const query = useAudit(navigation.cursor);
  const columns: ColumnDef<AuditRow>[] = [
    {
      accessorKey: "occurred_at",
      header: () => <Hint text={hints.audit.time}>Time</Hint>,
      cell: ({ getValue }) => formatDate(String(getValue())),
    },
    {
      accessorKey: "type",
      header: () => <Hint text={hints.audit.record}>Record</Hint>,
      cell: ({ getValue }) => <Badge>{humanize(String(getValue()))}</Badge>,
    },
    {
      id: "record_id",
      header: () => <Hint text={hints.audit.identity}>Identity</Hint>,
      cell: ({ row }) => (
        <span className="font-mono text-xs">
          {shortId(String(row.original.data.record_id ?? row.original.id))}
        </span>
      ),
    },
    {
      id: "digest",
      header: () => <Hint text={hints.audit.digest}>Digest</Hint>,
      cell: ({ row }) => (
        <span className="font-mono text-xs text-slate-500">
          {shortId(String(row.original.data.digest ?? "—"))}
        </span>
      ),
    },
  ];
  return (
    <>
      <PageHeader
        title="Audit"
        description="Immutable intents, receipts, actors and integrity-relevant state changes."
      />
      <QueryContent query={query}>
        <DataTable columns={columns} data={query.data?.items ?? []} />
        <CursorPager navigation={navigation} nextCursor={query.data?.next_cursor} />
      </QueryContent>
    </>
  );
}

export function NotFoundPage() {
  return (
    <Empty>
      <p>That control view does not exist.</p>
      <Link
        className="mt-4 inline-flex items-center gap-2 text-cyan-300 hover:underline"
        to="/"
      >
        Return to overview <ArrowRight size={15} />
      </Link>
    </Empty>
  );
}
