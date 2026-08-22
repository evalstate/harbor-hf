// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  affectedQueryKeys,
  collectPagedItems,
  JOBS_REFRESH_INTERVAL_MS,
  keys,
  useAllProfiles,
  useJobs,
  useLiveUpdates,
} from "../src/queries";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  open() {
    this.onopen?.();
  }

  message(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) } as MessageEvent<string>);
  }
}

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useLiveUpdates(true), { wrapper });
  return { client, hook, invalidate };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
});

describe("live query updates", () => {
  it("maps typed events without ever targeting the session", () => {
    const affected = affectedQueryKeys({
      type: "attempt.receipt",
      occurred_at: "2026-08-18T00:00:00Z",
      data: { campaign_id: "campaign-1", task_id: "task-1" },
    });
    expect(affected).toContainEqual(keys.campaigns);
    expect(affected).toContainEqual(keys.campaign("campaign-1"));
    expect(affected).toContainEqual(keys.capacity("campaign-1"));
    expect(affected).toContainEqual(keys.tasks("campaign-1"));
    expect(affected).toContainEqual(keys.task("campaign-1", "task-1"));
    expect(affected).not.toContainEqual(keys.session);
    expect(affected).not.toContainEqual(keys.results);
  });

  it("invalidates every capacity view after capacity profile promotion", () => {
    const affected = affectedQueryKeys({
      type: "profile.promotion",
      occurred_at: "2026-08-18T00:00:00Z",
      data: { profile_kind: "capacity", alias: "current" },
    });
    expect(affected).toContainEqual(["capacity"]);
    expect(affected).toContainEqual(keys.profiles);
  });

  it("targets capacity views for Sandbox admission records", () => {
    const affected = affectedQueryKeys({
      type: "sandbox.admission",
      occurred_at: "2026-08-18T00:00:00Z",
      data: { campaign_id: "campaign-1", action_id: "action-1" },
    });
    expect(affected).toContainEqual(keys.capacity("campaign-1"));
    expect(affected).toContainEqual(keys.campaign("campaign-1"));
    expect(affected).not.toContainEqual(keys.session);
  });

  it("refreshes open detail views for replay events without scope fields", () => {
    const affected = affectedQueryKeys({
      type: "attempt.receipt",
      occurred_at: "2026-08-18T00:00:00Z",
      data: { key: "control/example", digest: "sha256:example" },
    });
    expect(affected).toContainEqual(keys.campaigns);
    expect(affected).toContainEqual(["campaign"]);
    expect(affected).toContainEqual(["tasks"]);
    expect(affected).toContainEqual(["task"]);
  });

  it("resumes SSE from the projection cursor before page queries start", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    renderHook(() => useLiveUpdates(true, "cursor-one"), { wrapper });
    expect(FakeEventSource.instances[0]?.url).toBe("/api/v1/events?cursor=cursor-one");
  });

  it("does not poll while SSE stays connected", () => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", FakeEventSource);
    const { hook, invalidate } = setup();
    act(() => FakeEventSource.instances[0]?.open());
    invalidate.mockClear();

    for (let index = 0; index < 5; index += 1) {
      act(() => {
        vi.advanceTimersByTime(15_000);
        FakeEventSource.instances[0]?.message({
          type: "heartbeat",
          occurred_at: new Date().toISOString(),
          data: {},
        });
      });
    }

    expect(hook.result.current.status).toBe("connected");
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("uses the slow fallback only while disconnected and visible", () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
    vi.stubGlobal("EventSource", FakeEventSource);
    const { invalidate } = setup();

    act(() => vi.advanceTimersByTime(60_000));
    expect(invalidate).toHaveBeenCalled();
    invalidate.mockClear();
    visibility = "hidden";
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    act(() => vi.advanceTimersByTime(120_000));
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("reconnects with bounded backoff after an SSE failure", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    vi.stubGlobal("EventSource", FakeEventSource);
    const { hook } = setup();
    act(() => FakeEventSource.instances[0]?.open());
    act(() => FakeEventSource.instances[0]?.onerror?.());

    expect(hook.result.current.status).toBe("reconnecting");
    expect(FakeEventSource.instances).toHaveLength(1);
    act(() => vi.advanceTimersByTime(1_000));
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("invalidates only the queries affected by a control event", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const { invalidate } = setup();
    act(() => FakeEventSource.instances[0]?.open());
    invalidate.mockClear();
    act(() =>
      FakeEventSource.instances[0]?.message({
        type: "publication.receipt",
        occurred_at: "2026-08-18T00:00:00Z",
        data: { campaign_id: "campaign-1" },
      }),
    );

    const invalidated = invalidate.mock.calls.map(([options]) => options?.queryKey);
    expect(invalidated).toContainEqual(keys.results);
    expect(invalidated).toContainEqual(keys.campaigns);
    expect(invalidated).toContainEqual(keys.campaign("campaign-1"));
    expect(invalidated).toContainEqual(keys.audit);
    expect(invalidated).not.toContainEqual(keys.session);
    expect(invalidated).not.toContainEqual(keys.profiles);
  });

  it("refetches Jobs on a short interval so observed state stays current", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ items: [], next_cursor: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 60_000 } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    renderHook(() => useJobs(), { wrapper });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(JOBS_REFRESH_INTERVAL_MS);
    });
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("requests Jobs scoped to a campaign", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ items: [], next_cursor: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    renderHook(() => useJobs(undefined, "campaign-1"), { wrapper });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      "/api/v1/jobs?campaign_id=campaign-1",
    );
  });
});

describe("paged profile collection", () => {
  it("follows profile cursors until models and later harnesses appear", async () => {
    const pages = [
      {
        items: [{ name: "tb21-old-deployment" }, { name: "hermes" }],
        next_cursor: "page-2",
      },
      {
        items: [{ name: "opencode" }, { name: "gpt-oss-20b" }],
        next_cursor: null,
      },
    ];

    const items = await collectPagedItems(async (cursor) => {
      if (!cursor) return pages[0];
      if (cursor === "page-2") return pages[1];
      throw new Error(`unexpected cursor ${cursor}`);
    });

    expect(items.map((item) => item.name)).toEqual([
      "tb21-old-deployment",
      "hermes",
      "opencode",
      "gpt-oss-20b",
    ]);
  });

  it("loads every profile page for the launch form", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const page = url.includes("cursor=page-2")
        ? {
            items: [
              {
                profile_id: "model-1",
                profile_kind: "model",
                name: "gpt-oss-20b",
                approved_aliases: ["gpt-oss-20b"],
              },
              {
                profile_id: "harness-2",
                profile_kind: "harness",
                name: "opencode",
                approved_aliases: ["opencode"],
              },
            ],
            next_cursor: null,
          }
        : {
            items: [
              {
                profile_id: "deploy-1",
                profile_kind: "deployment",
                name: "tb21-old",
                approved_aliases: ["tb21-old"],
              },
            ],
            next_cursor: "page-2",
          };
      return new Response(JSON.stringify(page), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const hook = renderHook(() => useAllProfiles(), { wrapper });
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true));
    expect(hook.result.current.data?.items.map((item) => item.name)).toEqual([
      "tb21-old",
      "gpt-oss-20b",
      "opencode",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
