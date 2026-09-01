import { ImmutableConflictError } from "@harbor-hf/control-core";
import { beforeEach, describe, expect, it, vi } from "vitest";

const hub = vi.hoisted(() => ({
  listFiles: vi.fn(),
  uploadFile: vi.fn(),
}));
const bucketFetch = vi.fn();

vi.mock("@huggingface/hub", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@huggingface/hub")>()),
  ...hub,
}));

import { HuggingFaceBucketStore } from "../src/bucket-store.js";

const token = ["hf", "not-a-real-credential"].join("_");

function store(
  options: {
    retryDelaysMs?: readonly number[];
    listTimeoutMs?: number;
    cacheMaxBytes?: number;
  } = {},
) {
  return new HuggingFaceBucketStore({
    bucketId: "example/control",
    accessToken: token,
    fetch: bucketFetch as typeof fetch,
    ...options,
  });
}

describe("HuggingFaceBucketStore", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    bucketFetch.mockReset();
  });

  it("creates an object and verifies the uploaded bytes", async () => {
    bucketFetch
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(new Response("payload"));
    hub.uploadFile.mockResolvedValue(undefined);
    hub.listFiles.mockImplementation(async function* ({ path }: { path: string }) {
      yield {
        type: "file",
        path,
        size: 7,
        xetHash: "a".repeat(64),
      };
    });

    await expect(
      store().create("control/v1/object.json", new TextEncoder().encode("payload")),
    ).resolves.toMatchObject({
      created: true,
      source_identity: `xet:${"a".repeat(64)}`,
    });
    expect(hub.uploadFile).toHaveBeenCalledTimes(1);
    expect(hub.uploadFile.mock.calls[0]?.[0]).toMatchObject({
      repo: { type: "bucket", name: "example/control" },
      file: { path: "control/v1/object.json" },
    });
    expect(hub.listFiles).toHaveBeenCalledTimes(2);
    for (const [options] of hub.listFiles.mock.calls)
      expect(options).toMatchObject({
        path: "control/v1/object.json",
        recursive: false,
        expand: true,
      });
  });

  it("fails an upload when targeted metadata has no xet identity", async () => {
    bucketFetch.mockResolvedValueOnce(new Response(null, { status: 404 }));
    hub.uploadFile.mockResolvedValue(undefined);
    hub.listFiles.mockImplementation(async function* ({ path }: { path: string }) {
      yield {
        type: "file",
        path,
        size: 7,
        uploadedAt: "2026-08-24T10:00:00Z",
      };
    });

    await expect(
      store().create("control/v1/object.json", new TextEncoder().encode("payload")),
    ).rejects.toThrow("Bucket object has no valid xetHash");
    expect(hub.uploadFile).toHaveBeenCalledTimes(1);
    expect(hub.listFiles).toHaveBeenCalledWith(
      expect.objectContaining({
        path: "control/v1/object.json",
        recursive: false,
        expand: true,
      }),
    );
  });

  it("adopts identical objects and rejects immutable conflicts", async () => {
    bucketFetch
      .mockResolvedValueOnce(new Response("payload"))
      .mockResolvedValueOnce(new Response("payload"));
    hub.listFiles.mockImplementation(async function* ({ path }: { path: string }) {
      yield {
        type: "file",
        path,
        size: 7,
        xetHash: "a".repeat(64),
      };
    });
    await expect(
      store().create("control/v1/object.json", new TextEncoder().encode("payload")),
    ).resolves.toMatchObject({
      created: false,
      source_identity: `xet:${"a".repeat(64)}`,
    });

    bucketFetch.mockResolvedValueOnce(new Response("different"));
    await expect(
      store().create("control/v1/object.json", new TextEncoder().encode("payload")),
    ).rejects.toBeInstanceOf(ImmutableConflictError);
    expect(hub.uploadFile).not.toHaveBeenCalled();
  });

  it("rejects an overwrite between upload verification and metadata capture", async () => {
    bucketFetch
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(new Response("payload"));
    hub.uploadFile.mockResolvedValue(undefined);
    let listing = 0;
    hub.listFiles.mockImplementation(async function* ({ path }: { path: string }) {
      listing += 1;
      yield {
        type: "file",
        path,
        size: 7,
        xetHash: (listing === 1 ? "a" : "b").repeat(64),
      };
    });

    await expect(
      store().create("control/v1/object.json", new TextEncoder().encode("payload")),
    ).rejects.toBeInstanceOf(ImmutableConflictError);
  });

  it("lists Bucket metadata without downloading objects", async () => {
    hub.listFiles.mockImplementation(async function* () {
      for (let index = 0; index < 12; index += 1)
        yield {
          type: "file",
          path: `control/v1/${String(index).padStart(2, "0")}.json`,
          size: 1,
          xetHash: index.toString(16).padStart(64, "0"),
        };
    });

    const entries = await store().list("control/v1");

    expect(entries).toHaveLength(12);
    expect(bucketFetch).not.toHaveBeenCalled();
    expect(hub.listFiles).toHaveBeenCalledWith(
      expect.objectContaining({ expand: true, fetch: expect.any(Function) }),
    );
  });

  it("downloads an encoded Bucket path without a metadata round trip", async () => {
    bucketFetch.mockResolvedValueOnce(new Response("payload"));

    await expect(store().read("control/v1/a b.json")).resolves.toEqual(
      new TextEncoder().encode("payload"),
    );

    expect(bucketFetch).toHaveBeenCalledWith(
      new URL(
        "https://huggingface.co/buckets/example/control/resolve/control/v1/a%20b.json",
      ),
      {
        headers: { Authorization: `Bearer ${token}` },
        redirect: "follow",
      },
    );
    expect(hub.listFiles).not.toHaveBeenCalled();
  });

  it("invalidates cached bytes when listed source identity changes", async () => {
    const bucket = store();
    let identity = "a";
    hub.listFiles.mockImplementation(async function* ({ path }: { path: string }) {
      yield {
        type: "file",
        path: `${path}/object.json`,
        size: 3,
        xetHash: identity.repeat(64),
      };
    });
    bucketFetch
      .mockResolvedValueOnce(new Response("old"))
      .mockResolvedValueOnce(new Response("new"));

    await bucket.list("control/v1");
    await expect(bucket.read("control/v1/object.json")).resolves.toEqual(
      new TextEncoder().encode("old"),
    );
    await expect(bucket.read("control/v1/object.json")).resolves.toEqual(
      new TextEncoder().encode("old"),
    );
    expect(bucketFetch).toHaveBeenCalledTimes(1);

    identity = "b";
    await bucket.list("control/v1");
    await expect(bucket.read("control/v1/object.json")).resolves.toEqual(
      new TextEncoder().encode("new"),
    );
    expect(bucketFetch).toHaveBeenCalledTimes(2);
  });

  it("evicts least-recently-used bytes above the cache limit", async () => {
    const bucket = store({ cacheMaxBytes: 3 });
    bucketFetch
      .mockResolvedValueOnce(new Response("one"))
      .mockResolvedValueOnce(new Response("two"))
      .mockResolvedValueOnce(new Response("one"));

    await bucket.read("control/v1/one.json");
    await bucket.read("control/v1/two.json");
    await expect(bucket.read("control/v1/one.json")).resolves.toEqual(
      new TextEncoder().encode("one"),
    );

    expect(bucketFetch).toHaveBeenCalledTimes(3);
  });

  it("retries transient Bucket listing failures", async () => {
    hub.listFiles
      .mockImplementationOnce(async function* () {
        yield* [];
        throw new TypeError("fetch failed");
      })
      .mockImplementationOnce(async function* () {
        yield {
          type: "file",
          path: "control/v1/object.json",
          size: 1,
          xetHash: "a".repeat(64),
        };
      });

    await expect(
      store({ retryDelaysMs: [0] }).list("control/v1"),
    ).resolves.toHaveLength(1);
    expect(hub.listFiles).toHaveBeenCalledTimes(2);
  });

  it("rejects invalid Bucket list timeouts", () => {
    expect(() => store({ listTimeoutMs: 0 })).toThrow(
      "Bucket list timeout must be a positive integer",
    );
  });

  it("rejects invalid Bucket cache limits", () => {
    expect(() => store({ cacheMaxBytes: -1 })).toThrow(
      "Bucket cache limit must be a nonnegative integer",
    );
  });

  it("retries transient fetch failures with bounded delays", async () => {
    bucketFetch
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockRejectedValueOnce(
        Object.assign(new Error("temporary timeout"), { code: "ETIMEDOUT" }),
      )
      .mockResolvedValueOnce(new Response("payload"));

    await expect(
      store({ retryDelaysMs: [0, 0] }).read("control/v1/object.json"),
    ).resolves.toEqual(new TextEncoder().encode("payload"));
    expect(bucketFetch).toHaveBeenCalledTimes(3);
  });

  it("keeps retrying transient fetch failures during a long rebuild", async () => {
    vi.useFakeTimers();
    try {
      bucketFetch
        .mockRejectedValueOnce(new TypeError("fetch failed"))
        .mockRejectedValueOnce(new TypeError("fetch failed"))
        .mockRejectedValueOnce(new TypeError("fetch failed"))
        .mockRejectedValueOnce(new TypeError("fetch failed"))
        .mockRejectedValueOnce(new TypeError("fetch failed"))
        .mockResolvedValueOnce(new Response("payload"));

      const reading = store().read("control/v1/object.json");
      await vi.runAllTimersAsync();
      await expect(reading).resolves.toEqual(new TextEncoder().encode("payload"));
      expect(bucketFetch).toHaveBeenCalledTimes(6);
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries transient Hub API download failures", async () => {
    bucketFetch
      .mockResolvedValueOnce(new Response(null, { status: 504 }))
      .mockResolvedValueOnce(new Response("payload"));

    await expect(
      store({ retryDelaysMs: [0] }).read("control/v1/object.json"),
    ).resolves.toEqual(new TextEncoder().encode("payload"));
    expect(bucketFetch).toHaveBeenCalledTimes(2);
  });

  it("retries transient failures while materializing a lazy Blob", async () => {
    bucketFetch
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        arrayBuffer: async () => {
          throw new TypeError("terminated", {
            cause: Object.assign(new Error("socket closed"), {
              code: "UND_ERR_SOCKET",
            }),
          });
        },
      } as Response)
      .mockResolvedValueOnce(new Response("payload"));

    await expect(
      store({ retryDelaysMs: [0] }).read("control/v1/object.json"),
    ).resolves.toEqual(new TextEncoder().encode("payload"));
    expect(bucketFetch).toHaveBeenCalledTimes(2);
  });

  it("does not retry non-transient download failures", async () => {
    bucketFetch.mockResolvedValue(new Response(null, { status: 403 }));

    await expect(
      store({ retryDelaysMs: [0, 0] }).read("control/v1/object.json"),
    ).rejects.toThrow("Bucket object download failed with HTTP 403");
    expect(bucketFetch).toHaveBeenCalledTimes(1);
  });

  it("lists file keys and sizes in deterministic order", async () => {
    hub.listFiles.mockImplementation(async function* () {
      yield {
        type: "file",
        path: "control/v1/z.json",
        size: 1,
        xetHash: "A".repeat(64),
      };
      yield { type: "directory", path: "control/v1/nested", size: 0 };
      yield {
        type: "file",
        path: "control/v1/a.json",
        size: 1,
        xetHash: "b".repeat(64),
      };
    });
    const entries = await store().list("control/v1");

    expect(entries).toEqual([
      {
        key: "control/v1/a.json",
        size: 1,
        source_identity: `xet:${"b".repeat(64)}`,
      },
      {
        key: "control/v1/z.json",
        size: 1,
        source_identity: `xet:${"a".repeat(64)}`,
      },
    ]);
    expect(bucketFetch).not.toHaveBeenCalled();
  });

  it.each([
    [{ type: "file", path: "control/v1/missing.json", size: 1 }],
    [
      {
        type: "file",
        path: "control/v1/invalid-xet.json",
        size: 1,
        xetHash: "not-a-hash",
        uploadedAt: "2026-08-24T10:00:00Z",
      },
    ],
    [
      {
        type: "file",
        path: "control/v1/upload-time-only.json",
        size: 1,
        uploadedAt: "2026-08-24T10:00:00Z",
      },
    ],
  ])("rejects a Bucket file without a valid identity", async (entry) => {
    hub.listFiles.mockImplementation(async function* () {
      yield entry;
    });

    await expect(store().list("control/v1")).rejects.toThrow();
    expect(bucketFetch).not.toHaveBeenCalled();
  });
});
