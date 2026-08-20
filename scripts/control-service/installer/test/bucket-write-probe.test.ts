import { downloadFile, uploadFile } from "@huggingface/hub";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HuggingFaceBucketWriteProbe } from "../bucket-write-probe.js";

vi.mock("@huggingface/hub", () => ({
  downloadFile: vi.fn(),
  uploadFile: vi.fn(),
}));

const mockedUpload = vi.mocked(uploadFile);
const mockedDownload = vi.mocked(downloadFile);

describe("Bucket write probe", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("uploads a new path with the proposed token and verifies exact bytes", async () => {
    const bytes = new TextEncoder().encode("probe\n");
    mockedUpload.mockResolvedValue({
      commit: { oid: "a".repeat(40), url: "https://huggingface.co/commit-placeholder" },
      hookOutput: "",
    });
    mockedDownload.mockResolvedValue(new Blob([bytes]));

    await new HuggingFaceBucketWriteProbe().createAndVerify({
      bucketId: "example/control-artifacts",
      accessToken: "proposed-control-placeholder",
      path: "installer/write-probes/schema=v1/install/probe",
      bytes,
    });

    expect(mockedUpload).toHaveBeenCalledOnce();
    expect(mockedUpload.mock.calls[0]?.[0]).toMatchObject({
      repo: { type: "bucket", name: "example/control-artifacts" },
      accessToken: "proposed-control-placeholder",
      file: { path: "installer/write-probes/schema=v1/install/probe" },
    });
    expect(mockedDownload).toHaveBeenCalledWith({
      repo: { type: "bucket", name: "example/control-artifacts" },
      path: "installer/write-probes/schema=v1/install/probe",
      accessToken: "proposed-control-placeholder",
    });
  });

  it("rejects missing or changed read-back content", async () => {
    const probe = new HuggingFaceBucketWriteProbe();
    mockedUpload.mockResolvedValue({
      commit: { oid: "a".repeat(40), url: "https://huggingface.co/commit-placeholder" },
      hookOutput: "",
    });
    mockedDownload.mockResolvedValueOnce(null);
    await expect(
      probe.createAndVerify({
        bucketId: "example/control-artifacts",
        accessToken: "proposed-control-placeholder",
        path: "installer/write-probes/schema=v1/install/first",
        bytes: new Uint8Array([1]),
      }),
    ).rejects.toThrow("missing");

    mockedDownload.mockResolvedValueOnce(new Blob([new Uint8Array([2])]));
    await expect(
      probe.createAndVerify({
        bucketId: "example/control-artifacts",
        accessToken: "proposed-control-placeholder",
        path: "installer/write-probes/schema=v1/install/second",
        bytes: new Uint8Array([1]),
      }),
    ).rejects.toThrow("does not match");
  });
});
