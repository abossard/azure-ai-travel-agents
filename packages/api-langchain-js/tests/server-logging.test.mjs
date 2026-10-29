import assert from "node:assert/strict";
import { once } from "node:events";
import { readFileSync } from "node:fs";
import Module, { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { MCPClient } from "../src/mcp/mcp-http-client.ts";

const require = createRequire(import.meta.url);
const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function loadWithMocks(filename, mocks) {
  const module = new Module(filename);
  module.filename = filename;
  module.paths = Module._nodeModulePaths(path.dirname(filename));
  const originalRequire = module.require.bind(module);
  module.require = (name) => Object.hasOwn(mocks, name) ? mocks[name] : originalRequire(name);
  const source = readFileSync(filename, "utf8");
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  module._compile(code, filename);
  return module.exports;
}

test("chat and MCP tool content is not written to console logs", async () => {
  const userSentinel = "private-user-content-sentinel";
  const snapshotSentinel = "private-graph-snapshot-sentinel";
  const toolSentinel = "private-tool-content-sentinel";
  const express = require("express");
  let server;
  const fakeExpress = Object.assign(() => {
    const app = express();
    const listen = app.listen.bind(app);
    app.listen = (...args) => {
      server = listen(...args);
      return server;
    };
    return app;
  }, express);
  const logs = [];
  const originalLog = console.log;
  const originalError = console.error;
  const originalPort = process.env.PORT;
  console.log = (...args) => logs.push(args);
  console.error = (...args) => logs.push(args);
  process.env.PORT = "0";

  try {
    loadWithMocks(path.join(packageRoot, "src/server.ts"), {
      express: fakeExpress,
      dotenv: { config: () => ({}) },
      "./index.js": {
        setupAgents: async () => ({
          run: async function* (message) {
            assert.equal(message, userSentinel);
            yield { eventName: "on_chain_start", data: { agent: "TravelAgent", snapshot: snapshotSentinel } };
            yield { eventName: "tool_end", data: { agent: "TravelAgent", tool: "fixture", output: toolSentinel } };
          },
        }),
      },
      "./tools/index.js": { McpToolsConfig: () => ({}) },
      "./mcp/mcp-tools.js": { mcpToolsList: async () => [] },
    });
    await once(server, "listening");
    const response = await fetch(`http://127.0.0.1:${server.address().port}/api/chat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message: userSentinel, tools: [{ id: "fixture", input: toolSentinel }] }),
    });
    assert.equal(response.status, 200);
    const body = await response.text();
    assert.ok(body.includes(snapshotSentinel));
    assert.ok(body.includes(toolSentinel));

    const client = new MCPClient("fixture", "http://localhost:5000/mcp");
    Reflect.set(client, "client", { callTool: async () => ({}) });
    await client.callTool("fixture", JSON.stringify({ input: toolSentinel }));

    const telemetryLogs = [];
    const { MCPClient: SSEClient } = loadWithMocks(path.join(packageRoot, "src/mcp/mcp-sse-client.ts"), {
      "@modelcontextprotocol/sdk/client/index.js": {
        Client: class { async callTool() { return { content: toolSentinel }; } },
      },
      "@modelcontextprotocol/sdk/client/sse.js": { SSEClientTransport: class {} },
      "../utils/instrumentation.js": {
        tracer: { startActiveSpan: (_name, callback) => callback({ end() {} }) },
        log: (...args) => telemetryLogs.push(args),
      },
    });
    const sseClient = new SSEClient("fixture", "http://localhost:5000/mcp");
    await sseClient.callTool("fixture", { input: toolSentinel });

    const captured = JSON.stringify([...logs, ...telemetryLogs]);
    for (const sentinel of [userSentinel, snapshotSentinel, toolSentinel]) {
      assert.equal(captured.includes(sentinel), false);
    }
  } finally {
    if (server?.listening) await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    console.log = originalLog;
    console.error = originalError;
    if (originalPort === undefined) delete process.env.PORT;
    else process.env.PORT = originalPort;
  }
});

for (const local of [false, true]) {
  test(`Azure provider disables verbose model logging (${local ? "API key" : "managed identity"})`, async () => {
    const names = [
      "IS_LOCAL_DOCKER_ENV",
      "AZURE_OPENAI_ENDPOINT",
      "AZURE_OPENAI_API_KEY",
      "AZURE_OPENAI_DEPLOYMENT_NAME",
      "AZURE_CLIENT_ID",
    ];
    const previous = Object.fromEntries(names.map((name) => [name, process.env[name]]));
    process.env.IS_LOCAL_DOCKER_ENV = String(local);
    process.env.AZURE_OPENAI_ENDPOINT = "https://example.invalid";
    process.env.AZURE_OPENAI_API_KEY = "test-key";
    process.env.AZURE_OPENAI_DEPLOYMENT_NAME = "test-deployment";
    process.env.AZURE_CLIENT_ID = "test-client";

    try {
      const { llm } = loadWithMocks(path.join(packageRoot, "src/providers/azure-openai.ts"), {
        "@langchain/openai": { ChatOpenAI: class { constructor(options) { this.options = options; } } },
        "@azure/identity": {
          DefaultAzureCredential: class {},
          ManagedIdentityCredential: class {},
          getBearerTokenProvider: () => async () => "test-token",
        },
      });
      assert.equal((await llm()).options.verbose, false);
    } finally {
      for (const name of names) {
        if (previous[name] === undefined) delete process.env[name];
        else process.env[name] = previous[name];
      }
    }
  });
}
