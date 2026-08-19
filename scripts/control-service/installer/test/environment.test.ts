import { describe, expect, it } from "vitest";
import { sanitizedChildEnvironment } from "../environment.js";

describe("installer child environment", () => {
  it("never inherits installer secret sources", () => {
    const previous = {
      control: process.env.HARBOR_HF_INSTALL_CONTROL_SECRET,
      inference: process.env.HARBOR_HF_INSTALL_INFERENCE_SECRET,
      verify: process.env.HARBOR_HF_INSTALL_VERIFY_BEARER,
      hfToken: process.env.HF_TOKEN,
      unrelated: process.env.UNRELATED_CLOUD_SECRET,
    };
    process.env.HARBOR_HF_INSTALL_CONTROL_SECRET = "control-placeholder";
    process.env.HARBOR_HF_INSTALL_INFERENCE_SECRET = "inference-placeholder";
    process.env.HARBOR_HF_INSTALL_VERIFY_BEARER = "verify-placeholder";
    process.env.HF_TOKEN = "ambient-placeholder";
    process.env.UNRELATED_CLOUD_SECRET = "cloud-placeholder";
    try {
      const environment = sanitizedChildEnvironment();
      expect(environment.HARBOR_HF_INSTALL_CONTROL_SECRET).toBeUndefined();
      expect(environment.HARBOR_HF_INSTALL_INFERENCE_SECRET).toBeUndefined();
      expect(environment.HARBOR_HF_INSTALL_VERIFY_BEARER).toBeUndefined();
      expect(environment.HF_TOKEN).toBeUndefined();
      expect(environment.UNRELATED_CLOUD_SECRET).toBeUndefined();
    } finally {
      for (const [name, value] of [
        ["HARBOR_HF_INSTALL_CONTROL_SECRET", previous.control],
        ["HARBOR_HF_INSTALL_INFERENCE_SECRET", previous.inference],
        ["HARBOR_HF_INSTALL_VERIFY_BEARER", previous.verify],
        ["HF_TOKEN", previous.hfToken],
        ["UNRELATED_CLOUD_SECRET", previous.unrelated],
      ] as const) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
  });
});
