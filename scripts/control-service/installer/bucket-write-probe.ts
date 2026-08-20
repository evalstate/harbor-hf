import { downloadFile, uploadFile } from "@huggingface/hub";

export interface BucketWriteProbeInput {
  bucketId: string;
  accessToken: string;
  path: string;
  bytes: Uint8Array;
}

export interface BucketWriteProbeAdapter {
  createAndVerify(input: BucketWriteProbeInput): Promise<void>;
}

export class HuggingFaceBucketWriteProbe implements BucketWriteProbeAdapter {
  async createAndVerify(input: BucketWriteProbeInput): Promise<void> {
    const repo = { type: "bucket" as const, name: input.bucketId };
    await uploadFile({
      repo,
      file: {
        path: input.path,
        content: new Blob([Uint8Array.from(input.bytes).buffer]),
      },
      commitTitle: "Verify Harbor-HF installer Bucket write access",
      accessToken: input.accessToken,
    });
    const observed = await downloadFile({
      repo,
      path: input.path,
      accessToken: input.accessToken,
    });
    if (!observed) throw new Error("Bucket write probe is missing");
    const bytes = new Uint8Array(await observed.arrayBuffer());
    if (
      bytes.byteLength !== input.bytes.byteLength ||
      bytes.some((value, index) => value !== input.bytes[index])
    ) {
      throw new Error("Bucket write probe content does not match");
    }
  }
}
