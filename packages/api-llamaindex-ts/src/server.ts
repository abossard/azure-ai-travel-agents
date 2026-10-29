import dotenv from "dotenv";
dotenv.config();

import cors from "cors";
import express from "express";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { setupAgents } from "./index.js";
import { McpToolsConfig } from "./tools/index.js";
import { mcpToolsList } from "./mcp/mcp-tools.js";
import { agent } from "@llamaindex/workflow";

const app = express();
const PORT = process.env.PORT || 4001;
const CHUNK_END = "\n\n";

// Middleware
app.use(cors());
app.use(express.json());

const apiRouter = express.Router();

// Add request body logging middleware for debugging
apiRouter.use((req, res, next) => {
  if (req.path === "/chat" && req.method === "POST") {
    const contentType = req.headers["content-type"]?.replace(/\n|\r/g, "");
    const body =
      typeof req.body === "string"
        ? req.body.replace(/\n|\r/g, "")
        : JSON.stringify(req.body).replace(/\n|\r/g, "");
    console.log("Request Content-Type:", contentType);
    console.log("Request body:", body);
  }
  next();
});

// Health check endpoint
apiRouter.get("/health", (req, res) => {
  res.status(200).json({ 
    status: "OK",
    orchestrator: "llamaindex-ts"
  });
});

// MCP tools
apiRouter.get("/tools", async (req, res) => {
  try {
    const tools = await mcpToolsList(Object.values(McpToolsConfig()));
    console.log("Available tools:", tools);
    res.status(200).json({ tools });
  } catch (error) {
    console.error("Error fetching MCP tools:", error);
    res.status(500).json({ error: "Error fetching MCP tools" });
  }
});

// Chat endpoint with Server-Sent Events (SSE) for streaming responses
// @ts-ignore - Ignoring TypeScript errors for Express route handlers
apiRouter.post("/chat", async (req, res) => {
  let clientDisconnected = false;
  res.on("close", () => {
    if (!res.writableEnded) {
      clientDisconnected = true;
      console.log("Client disconnected, aborting...");
    }
  });

  if (!req.body) {
    console.error(
      "Request body is undefined. Check Content-Type header in the request."
    );
    return res.status(400).json({
      error:
        "Request body is undefined. Make sure to set Content-Type to application/json.",
    });
  }

  const message = req.body.message;
  const tools = req.body.tools;
  console.log("Tools to use:", JSON.stringify(tools, null, 2));

  if (!message) {
    return res.status(400).json({ error: "Message is required" });
  }

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");

  try {
    const workflow = await setupAgents(tools);
    const context = workflow.run(message);

    const readableStream = Readable.from((async function* () {
      try {
        for await (const event of context) {
          if (event.eventName === "error") {
            yield JSON.stringify({
              type: "error",
              message: typeof event.data === "string" ? event.data : "Workflow failed",
            }) + CHUNK_END;
            continue;
          }
          const eventType = event.type ?? event.eventName;
          const data = eventType === "4"
            ? {
                agent: event.data?.agent,
                currentAgentName: event.data?.currentAgentName,
                delta: event.data?.delta,
              }
            : event.data;
          yield JSON.stringify({
            type: "metadata",
            kind: "llamaindex-ts",
            event: { type: eventType, data },
            data: data ?? null,
          }) + CHUNK_END;
        }
      } catch (error: unknown) {
        if (clientDisconnected && error instanceof Error && "code" in error && error.code === "ERR_STREAM_PREMATURE_CLOSE") {
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        console.error("Error during streaming:", message);
        yield JSON.stringify({ type: "error", message }) + CHUNK_END;
      }
    })(), { objectMode: false });

    await pipeline(readableStream, res);
  } catch (error) {
    if (clientDisconnected && error instanceof Error && "code" in error && error.code === "ERR_STREAM_PREMATURE_CLOSE") {
      return;
    }
    console.error("Error occurred:", error);
    if (!res.headersSent) {
      res.status(500).json({ error: (error as any).message });
    } else if (!res.destroyed) {
      res.write(
        `${JSON.stringify({
          type: "error",
          message: (error as any).message,
        })}` + CHUNK_END
      );
      res.end();
    }
  }
});

// Mount the API router with the /api prefix
app.use("/api", apiRouter);

// Add a root route for API information
app.get("/", (req, res) => {
  res.json({
    message: "AI Travel Agents API - LlamaIndex.TS",
    version: "1.0.0",
    orchestrator: "llamaindex-ts",
    endpoints: {
      health: "/api/health",
      tools: "/api/tools",
      chat: "/api/chat",
    },
  });
});

// Start the server
app.listen(PORT, () => {
  console.log(`LlamaIndex.TS API server running on port ${PORT}`);
  console.log(`API endpoints:`);
  console.log(`  - Health check: http://localhost:${PORT}/api/health (GET)`);
  console.log(`  - MCP Tools: http://localhost:${PORT}/api/tools (GET)`);
  console.log(`  - Chat: http://localhost:${PORT}/api/chat (POST)`);
});
