import { describe, expect, it } from "vitest";
import {
  compileAgentWorkbenchRecipe,
  fastAgentWorkbenchStarter,
  fxWorkbenchStarter,
} from "../src/workbench.js";

describe("Agent Workbench recipe compiler", () => {
  it("compiles the Fast-Agent starter into one generic command-agent profile", () => {
    const preview = compileAgentWorkbenchRecipe(fastAgentWorkbenchStarter);
    expect(preview.recipe.name).toBe("fast-agent");
    expect(preview.setup_command).toContain("uv_version=0.12.5");
    expect(preview.setup_command).toContain(
      "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2",
    );
    expect(preview.setup_command).toContain("python_version=3.12.14");
    expect(preview.setup_command).toContain("fast-agent-mcp==0.10.11");
    expect(preview.setup_command).not.toContain('python -m venv "$AGENT_HOME/venv"');
    expect(preview.run_command).toContain("--base-url");
    expect(preview.run_command).toContain("http://127.0.0.1:18080/v1");
    expect(preview.environment.find((item) => item.name === "OPENAI_API_KEY")).toEqual(
      expect.objectContaining({
        value: "<injected-placeholder>",
        redacted: true,
      }),
    );
    expect(
      preview.environment.find((item) => item.name === "GENERIC_API_KEY"),
    ).toBeUndefined();
    expect(preview.harness_profile).toMatchObject({
      agent: "command-agent",
      required_evidence: ["workspace", "verifier", "provider-usage", "trajectory"],
      harbor_agent: {
        import_path: "harbor_hf_agents.command_agent.agent:CommandAgent",
        override_setup_timeout_sec: 1800,
        kwargs: {
          config: {
            route_api: "chat-completions",
            outputs: [{ path: "fast-agent-results.json" }],
          },
        },
      },
    });
  });

  it("compiles the FX starter through the same generic command-agent path", () => {
    const preview = compileAgentWorkbenchRecipe(fxWorkbenchStarter);
    expect(preview.recipe.name).toBe("fx");
    expect(preview.setup_command).toContain("https://releases.fx.sh/v0.0.6/fx-linux-");
    expect(preview.run_command).toContain('fx" ask --yolo --json --');
    expect(preview.harness_profile).toMatchObject({
      agent: "command-agent",
      required_evidence: ["workspace", "verifier", "provider-usage"],
      harbor_agent: {
        import_path: "harbor_hf_agents.command_agent.agent:CommandAgent",
        override_setup_timeout_sec: 600,
        kwargs: {
          config: {
            route_api: "chat-completions",
            outputs: [{ path: "fx-results.json" }],
          },
        },
      },
    });
  });

  it("produces stable identities and changes them with behavior", () => {
    const first = compileAgentWorkbenchRecipe(fastAgentWorkbenchStarter);
    const second = compileAgentWorkbenchRecipe(
      structuredClone(fastAgentWorkbenchStarter),
    );
    const changed = compileAgentWorkbenchRecipe({
      ...structuredClone(fastAgentWorkbenchStarter),
      setup_timeout_seconds: 1700,
    });
    expect(second.recipe_digest).toBe(first.recipe_digest);
    expect(second.revision_id).toBe(first.revision_id);
    expect(changed.recipe_digest).not.toBe(first.recipe_digest);
    expect(changed.revision_id).not.toBe(first.revision_id);
  });

  it("rejects duplicate, reserved, and credential-like literals", () => {
    expect(() =>
      compileAgentWorkbenchRecipe({
        ...structuredClone(fastAgentWorkbenchStarter),
        environment: [
          { name: "DUPLICATE", source: "literal", value: "a" },
          { name: "DUPLICATE", source: "literal", value: "b" },
        ],
      }),
    ).toThrow("duplicated");
    expect(() =>
      compileAgentWorkbenchRecipe({
        ...structuredClone(fastAgentWorkbenchStarter),
        environment: [{ name: "HF_TOKEN", source: "literal", value: "value" }],
      }),
    ).toThrow("reserved");
    expect(() =>
      compileAgentWorkbenchRecipe({
        ...structuredClone(fastAgentWorkbenchStarter),
        environment: [
          {
            name: "SERVICE_API_KEY",
            source: "literal",
            value: "not-a-secret",
          },
        ],
      }),
    ).toThrow("credential-like");
    expect(() =>
      compileAgentWorkbenchRecipe({
        ...structuredClone(fastAgentWorkbenchStarter),
        setup_command: `printf '%s' '${["hf", "not-a-real-token-value"].join("_")}'`,
      }),
    ).toThrow("credential-like");
    expect(() =>
      compileAgentWorkbenchRecipe({
        ...structuredClone(fastAgentWorkbenchStarter),
        environment: [
          {
            name: "CONFIG",
            source: "literal",
            value: ["hf", "not-a-real-token-value"].join("_"),
          },
        ],
      }),
    ).toThrow("credential");
  });

  it("keeps instructions as a path binding instead of command text", () => {
    const preview = compileAgentWorkbenchRecipe(fastAgentWorkbenchStarter);
    expect(preview.run_command).toContain("/run/agent/instruction.txt");
    expect(JSON.stringify(preview.harness_profile)).not.toContain("Setup test only");
  });

  it("rejects run-only bindings from setup", () => {
    expect(() =>
      compileAgentWorkbenchRecipe({
        ...structuredClone(fastAgentWorkbenchStarter),
        setup_command: 'curl "$MODEL_BASE_URL"',
      }),
    ).toThrow("run-only environment variable MODEL_BASE_URL");
  });
});
