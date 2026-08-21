// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import { ApiError, type SessionResponse } from "../src/api";
import { formatMoney } from "../src/lib";
import { keys } from "../src/queries";

class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  constructor() {
    queueMicrotask(() => this.onopen?.());
  }
  close() {}
}

function json(value: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function session(username = "test-user"): SessionResponse {
  return {
    authenticated: true,
    expires_at: "2026-08-18T12:00:00.000Z",
    actor: { username, role: "operator", transport: "development" },
  };
}

function system(writeMode: "disabled" | "canary" | "enabled" = "canary") {
  return {
    source_revision: "revision-0123456789abcdef",
    write_mode: writeMode,
    projection: {
      ready: true,
      rebuilding: false,
      object_count: 4,
      last_rebuild_at: "2026-08-18T00:00:00.000Z",
      last_sync_at: "2026-08-18T00:01:00.000Z",
      event_cursor: null,
      integrity_error: null,
    },
    resource_contract: { spaces: 1, buckets: 1, operator_secrets: 2 },
  };
}

function renderApp(path = "/", client?: QueryClient) {
  const queryClient =
    client ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client: queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("control web", () => {
  it("returns OAuth login to the current path without iframe query credentials", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ authenticated: false, login_url: "/auth/login" }, 401)),
    );
    renderApp(`/results?platform_access=${"x".repeat(600)}#private`);
    expect(
      await screen.findByRole("link", { name: /sign in with hugging face/i }),
    ).toHaveAttribute("href", "/auth/login?return_to=%2Fresults");
  });

  it("shows the username and never renders the OAuth subject", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("auth/session")) return json(session("visible-user"));
        if (path.includes("/system")) return json(system());
        if (path.includes("/campaigns")) return json({ items: [], next_cursor: null });
        if (path.includes("/endpoints")) return json({ items: [], next_cursor: null });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp();
    expect(await screen.findByText("visible-user")).toBeInTheDocument();
    expect(screen.queryByText("opaque-oauth-subject")).not.toBeInTheDocument();

    const detailsButton = screen.getByRole("button", {
      name: "Account and session details",
    });
    const detailsId = detailsButton.getAttribute("aria-describedby");
    const details = detailsId ? document.getElementById(detailsId) : null;
    expect(details).toHaveAttribute("role", "tooltip");
    expect(details).toHaveClass("invisible", "absolute");
    expect(details).toHaveTextContent("Operator role");
    expect(details).toHaveTextContent("Your role grants permission");
    expect(details).toHaveTextContent("Session expires");
  });

  it("keeps the authenticated shell and stale data after a transient session failure", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("/system")) return json(system());
        if (path.includes("/campaigns")) return json({ items: [], next_cursor: null });
        if (path.includes("/endpoints")) return json({ items: [], next_cursor: null });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(keys.session, session("cached-user"));
    renderApp("/", client);
    expect(await screen.findByText("cached-user")).toBeInTheDocument();

    act(() => {
      const query = client.getQueryCache().find({ queryKey: keys.session });
      if (!query) throw new Error("session query is missing");
      query.setState({
        ...query.state,
        error: new ApiError(
          429,
          "rate_limit_exceeded",
          "request rate limit exceeded",
          "safe-request-id",
          Date.now() + 60_000,
        ),
        status: "error",
        fetchStatus: "idle",
      });
    });

    expect(await screen.findByText("Showing saved data")).toBeInTheDocument();
    expect(screen.getByText("cached-user")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /sign in with hugging face/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/safe-request-id/)).toBeInTheDocument();
  });

  it("disables mutation controls when deployment writes are disabled", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("auth/session")) return json(session());
        if (path.includes("/system")) return json(system("disabled"));
        if (path.includes("/campaigns")) return json({ items: [], next_cursor: null });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp("/campaigns");
    expect(await screen.findByRole("button", { name: "Launch" })).toBeDisabled();
    expect(screen.getByText(/role grants permission/i)).toBeInTheDocument();
  });

  it("requires a separate acknowledgement before campaign cancellation", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("auth/session")) return json(session());
        if (path.includes("/system")) return json(system());
        if (path.endsWith("/api/v1/campaigns/campaign-1"))
          return json({
            campaign_id: "campaign-1",
            created_at: "2026-08-18T00:00:00.000Z",
            status: "active",
            publication_status: null,
            total_tasks: 3,
            terminal_tasks: 1,
            successful_tasks: 1,
            pending_actions: 1,
            observed_microusd: 1_000_000,
            reserved_microusd: 2_000_000,
            ceiling_microusd: 3_000_000,
            cleanup_pending: true,
          });
        if (path.includes("/api/v1/campaigns/campaign-1/tasks"))
          return json({ items: [], next_cursor: null });
        if (path.includes("/api/v1/jobs"))
          return json({ items: [], next_cursor: null });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp("/campaigns/campaign-1");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /cancel campaign/i }));
    const confirm = screen.getByRole("button", { name: /confirm cancellation/i });
    expect(confirm).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    expect(confirm).toBeEnabled();
  });

  it("lists campaign Jobs with Hub inspect links", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("auth/session")) return json(session());
        if (path.includes("/system")) return json(system());
        if (path.endsWith("/api/v1/campaigns/campaign-1"))
          return json({
            campaign_id: "campaign-1",
            created_at: "2026-08-18T00:00:00.000Z",
            status: "completed",
            publication_status: "published",
            total_tasks: 1,
            terminal_tasks: 1,
            successful_tasks: 1,
            pending_actions: 0,
            observed_microusd: 0,
            reserved_microusd: 0,
            ceiling_microusd: 0,
            cleanup_pending: false,
          });
        if (path.includes("/api/v1/campaigns/campaign-1/tasks/control-smoke-task"))
          return json({
            task: {
              campaign_id: "campaign-1",
              task_id: "control-smoke-task",
              input_digest: "sha256:aa",
              terminal_outcome: "complete",
              selected_attempt_id: "attempt-1",
            },
            attempts: [
              {
                attempt_id: "attempt-1",
                action_id: "action-job-1",
                campaign_id: "campaign-1",
                task_id: "control-smoke-task",
                outcome: "complete",
                replacement_eligible: false,
                cost_microusd: 0,
                metrics: { reward: 1 },
                created_at: "2026-08-18T00:00:00.000Z",
              },
            ],
          });
        if (path.includes("/api/v1/campaigns/campaign-1/tasks"))
          return json({ items: [], next_cursor: null });
        if (path.includes("/api/v1/jobs?campaign_id=campaign-1"))
          return json({
            items: [
              {
                action_id: "action-job-1",
                campaign_id: "campaign-1",
                action_kind: "job.observe",
                generation: 1,
                target: "693994e21a39f67af5a41ad0",
                outcome: "completed",
                observed_state: "COMPLETED",
                resource_id: "693994e21a39f67af5a41ad0",
                inspect_url:
                  "https://huggingface.co/jobs/test/693994e21a39f67af5a41ad0",
                created_at: "2026-08-18T00:00:00.000Z",
                cost_microusd: 1_000_000,
              },
            ],
            next_cursor: null,
          });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp("/campaigns/campaign-1/tasks/control-smoke-task");
    const link = await screen.findByRole("link", {
      name: /693994e21a39f67af5a41ad0/i,
    });
    expect(link).toHaveAttribute(
      "href",
      "https://huggingface.co/jobs/test/693994e21a39f67af5a41ad0",
    );
    expect(screen.getByRole("heading", { name: "Jobs" })).toBeInTheDocument();
  });

  it("links Jobs to the Hub inspect page", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("auth/session")) return json(session());
        if (path.includes("/system")) return json(system());
        if (path.includes("/api/v1/jobs"))
          return json({
            items: [
              {
                action_id: "action-job-1",
                campaign_id: "campaign-job-1",
                action_kind: "job.launch",
                generation: 1,
                target: "task-1",
                outcome: "created",
                observed_state: "RUNNING",
                resource_id: "693994e21a39f67af5a41ad0",
                inspect_url:
                  "https://huggingface.co/jobs/test/693994e21a39f67af5a41ad0",
                created_at: "2026-08-18T00:00:00.000Z",
                cost_microusd: 1_000_000,
              },
            ],
            next_cursor: null,
          });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp("/jobs");
    const link = await screen.findByRole("link", {
      name: /693994e21a39f67af5a41ad0/i,
    });
    expect(link).toHaveAttribute(
      "href",
      "https://huggingface.co/jobs/test/693994e21a39f67af5a41ad0",
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(screen.getByText(formatMoney(1_000_000))).toBeInTheDocument();
  });

  it("shows campaign request errors instead of a false not-found state", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("auth/session")) return json(session());
        if (path.includes("/system")) return json(system());
        if (path.endsWith("/api/v1/campaigns/campaign-error"))
          return json(
            {
              error: {
                code: "access_denied",
                message: "access denied",
                request_id: "request-campaign",
              },
            },
            403,
          );
        if (path.includes("/tasks")) return json({ items: [], next_cursor: null });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp("/campaigns/campaign-error");
    expect(await screen.findByText("Forbidden")).toBeInTheDocument();
    expect(screen.queryByText("Campaign not found")).not.toBeInTheDocument();
  });

  it("keeps collection cursors in the URL and loads later pages", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        requests.push(path);
        if (path.includes("auth/session")) return json(session());
        if (path.includes("/system")) return json(system());
        if (path.includes("/campaigns")) {
          const laterPage = path.includes("cursor=cursor-one");
          return json({
            items: [
              {
                campaign_id: laterPage ? "campaign-second" : "campaign-first",
                status: "active",
                terminal_tasks: 0,
                successful_tasks: 0,
                total_tasks: 1,
                observed_microusd: 0,
                ceiling_microusd: 0,
                created_at: "2026-08-16T00:00:00Z",
              },
            ],
            next_cursor: laterPage ? null : "cursor-one",
          });
        }
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp("/campaigns");
    const user = userEvent.setup();

    expect(await screen.findByText("campaign-first")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("campaign-second")).toBeInTheDocument();
    expect(requests.some((path) => path.includes("cursor=cursor-one"))).toBe(true);
  });

  it("labels finished campaigns with sealed failures separately from complete success", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("auth/session")) return json(session());
        if (path.includes("/system")) return json(system());
        if (path.includes("/campaigns"))
          return json({
            items: [
              {
                campaign_id: "campaign-success",
                status: "completed",
                terminal_tasks: 1,
                successful_tasks: 1,
                total_tasks: 1,
                observed_microusd: 0,
                ceiling_microusd: 0,
                created_at: "2026-08-16T00:00:00Z",
              },
              {
                campaign_id: "campaign-timeout",
                status: "completed",
                terminal_tasks: 2,
                successful_tasks: 1,
                total_tasks: 2,
                observed_microusd: 0,
                ceiling_microusd: 0,
                created_at: "2026-08-16T01:00:00Z",
              },
            ],
            next_cursor: null,
          });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp("/campaigns");
    expect(await screen.findByText("Completed with failures")).toBeInTheDocument();
    expect(screen.getByText("Completed with failures").className).toContain("amber");
    const successBadge = screen
      .getAllByText("Completed", { exact: true })
      .find((element) => element.tagName === "SPAN");
    expect(successBadge?.className).toContain("emerald");
  });

  it("explains launch policy on hover", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("auth/session")) return json(session());
        if (path.includes("/system")) return json(system());
        if (path.includes("/campaigns")) return json({ items: [], next_cursor: null });
        if (path.includes("/profiles"))
          return json({
            items: [
              {
                profile_id: "sha256:benchmark",
                profile_kind: "benchmark",
                name: "control-smoke",
                source: "built-in",
                promotion_state: "approved",
                alias: "control-smoke",
                approved_aliases: ["control-smoke"],
                spec: { task_ids: ["task-001"] },
                created_at: "2026-08-16T00:00:00.000Z",
              },
              {
                profile_id: "sha256:model",
                profile_kind: "model",
                name: "control-smoke",
                source: "built-in",
                promotion_state: "approved",
                alias: "control-smoke",
                approved_aliases: ["control-smoke"],
                spec: { revision: "sha256:model" },
                created_at: "2026-08-16T00:00:00.000Z",
              },
              {
                profile_id: "sha256:harness",
                profile_kind: "harness",
                name: "control-smoke",
                source: "built-in",
                promotion_state: "approved",
                alias: "control-smoke",
                approved_aliases: ["control-smoke"],
                spec: { agent: "control-smoke" },
                created_at: "2026-08-16T00:00:00.000Z",
              },
              {
                profile_id: "sha256:deployment",
                profile_kind: "deployment",
                name: "hf-cpu-smoke",
                source: "built-in",
                promotion_state: "approved",
                alias: "hf-cpu-smoke",
                approved_aliases: ["hf-cpu-smoke"],
                spec: {
                  models: ["control-smoke"],
                  harnesses: ["control-smoke"],
                  hardware: "cpu-basic",
                },
                created_at: "2026-08-16T00:00:00.000Z",
              },
              {
                profile_id: "sha256:policy",
                profile_kind: "launch_policy",
                name: "control-smoke",
                source: "built-in",
                promotion_state: "approved",
                alias: "control-smoke",
                approved_aliases: ["control-smoke"],
                spec: {
                  max_infrastructure_attempts: 1,
                  reservation_microusd: 0,
                  publication_role: "diagnostic",
                },
                created_at: "2026-08-16T00:00:00.000Z",
              },
            ],
            next_cursor: null,
          });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp("/campaigns");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Launch" }));
    expect(
      screen.getByText(/admission and repair rules/i, { hidden: true }),
    ).toBeInTheDocument();
  });

  it("keeps campaign completed distinct from a timed-out task", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.includes("auth/session")) return json(session());
        if (path.includes("/system")) return json(system());
        if (path.endsWith("/api/v1/campaigns/campaign-mixed"))
          return json({
            campaign_id: "campaign-mixed",
            created_at: "2026-08-18T00:00:00.000Z",
            status: "completed",
            publication_status: "published",
            total_tasks: 2,
            terminal_tasks: 2,
            successful_tasks: 1,
            pending_actions: 0,
            observed_microusd: 0,
            reserved_microusd: 0,
            ceiling_microusd: 0,
            cleanup_pending: false,
          });
        if (path.includes("/api/v1/campaigns/campaign-mixed/tasks"))
          return json({
            items: [
              {
                campaign_id: "campaign-mixed",
                task_id: "timeout-task",
                input_digest: "sha256:aa",
                terminal_outcome: "benchmark_timeout",
                selected_attempt_id: "attempt-timeout",
              },
              {
                campaign_id: "campaign-mixed",
                task_id: "complete-task",
                input_digest: "sha256:bb",
                terminal_outcome: "complete",
                selected_attempt_id: "attempt-complete",
              },
            ],
            next_cursor: null,
          });
        if (path.includes("/api/v1/jobs"))
          return json({ items: [], next_cursor: null });
        throw new Error(`unexpected request: ${path}`);
      }),
    );
    renderApp("/campaigns/campaign-mixed");
    expect(await screen.findByText("Completed with failures")).toBeInTheDocument();
    expect(
      screen.getByText("Published. 1 sealed task did not succeed."),
    ).toBeInTheDocument();
    expect(screen.getByText("Complete").className).toContain("emerald");
    expect(screen.getByText("Benchmark timeout").className).toContain("amber");
  });
});
