# Azure AI Travel Agents API (Python)

Python-based API server using **Microsoft Agent Framework** for multi-agent orchestration with **simplified MCP integration**.

The service uses Agent Framework 1.23.0. Some historical snippets below show earlier SDK APIs; the current implementation is in `src/orchestrator/`.

## Overview

This is the Python implementation of the orchestration layer using Microsoft Agent Framework (MAF). It provides a clean, maintainable API that relies entirely on MAF's built-in capabilities for MCP server integration and agent orchestration.

**Current Status: ✅ Fully Simplified - Production Ready**

The implementation strictly follows Microsoft Agent Framework best practices, using:
- Built-in `MCPStreamableHTTPTool` for all MCP integrations (no custom transports)
- Magentic orchestration with proper async lifecycle management
- True streaming events via asyncio.Queue
- Explicit errors when planned MCP tools are unavailable

**Key Simplification**: Removed ~300 lines of custom MCP transport code and all async lifecycle complexity by strictly following MAF patterns from official samples.

## Key Features

- ✅ **Microsoft Agent Framework** - Pure MAF SDK implementation, no custom code
- ✅ **Magentic Orchestration** - Multi-agent workflow coordination
- ✅ **Built-in MCP Support** - Using `MCPStreamableHTTPTool` exclusively
- ✅ **Real-time Streaming** - True SSE streaming with asyncio.Queue
- ✅ **Tool-aware errors** - Planned tool work cannot silently fall back to an LLM-only answer
- ✅ **Async Best Practices** - Proper lifecycle management, no cancel scope errors
- ✅ **OpenTelemetry Ready** - Built-in observability support
- ✅ **Multiple LLM Providers** - Azure OpenAI, GitHub Models, Ollama, Docker Models

## Architecture Highlights

### Simplified MCP Integration

**Before**: Custom transport implementations with complex async lifecycle
```python
# OLD - Complex custom transport (removed)
async with create_mcp_client(url) as (read, write, session):
    result = await session.call_tool(...)
```

**After**: Built-in MAF tools with automatic lifecycle management.

### Tool Registry Pattern

The `ToolRegistry` now only stores metadata - no persistent connections:
- Each workflow creates fresh MCP tool instances
- Tools are passed to agents at creation time
- MAF handles all connection lifecycle automatically
- Explicit errors when a planned tool is unavailable

### True Streaming

The Magentic workflow streams intermediate and final events to the chat API.

## Quick Start

### Prerequisites

- Python 3.11 or later
- Docker (for MCP servers)
- Azure OpenAI account (or other LLM provider)

### Installation

```bash
cd src/api-python

# Install dependencies
pip install -e .

# For development
pip install -e ".[dev]"
```

### Configuration

Create `.env` file from template:

```bash
cp .env.sample .env
```

**Minimal configuration:**

```txt
# LLM Provider
LLM_PROVIDER=azure-openai

# Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-api-key
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-5

# MCP Servers (Docker containers)
MCP_CUSTOMER_QUERY_URL=http://localhost:5001
MCP_DESTINATION_RECOMMENDATION_URL=http://localhost:5002
MCP_ITINERARY_PLANNING_URL=http://localhost:5003
MCP_ECHO_PING_URL=http://localhost:5004
```

### Run

```bash
# Start API server (from packages/api-maf-python)
uvicorn src.main:app --reload --port 4010 --log-level=debug
```

Server starts at `http://localhost:4010`


## API Endpoints

### GET /api/health

Health check with MCP server status.

**Response:**
```json
{
  "status": "OK",
  "service": "azure-ai-travel-agents-api-python",
  "version": "1.0.0",
  "llm_provider": "azure-openai",
  "mcp": {
    "total_servers": 3,
    "configured_servers": ["echo-ping", "customer-query", "itinerary-planning", "destination-recommendation"],
  }
}
```

### GET /api/tools

List all available MCP tools with reachability check.

**Response:**
```json
{
  "tools": [
    {
      "id": "customer-query",
      "name": "Customer Query",
      "url": "http://localhost:5001/mcp",
      "type": "http",
      "reachable": true,
      "selected": true,
      "tools": []
    }
  ]
}
```

### POST /api/chat

Process travel planning requests with **real-time streaming**.

**Request:**
```json
{
  "message": "Plan a 7-day trip to Paris",
  "context": {}
}
```

**Response:** Server-Sent Events (SSE) stream

```
data: {"type":"metadata","agent":"Orchestrator","event":"OrchestratorPlanning","data":{...}}

data: {"type":"metadata","agent":"CustomerQueryAgent","event":"AgentDelta","data":{"text":"Let me..."}}

data: {"type":"metadata","agent":"CustomerQueryAgent","event":"AgentMessage","data":{...}}

data: {"type":"metadata","agent":null,"event":"FinalResult","data":{"message":"Here's your plan..."}}
```

**Event Format:**
```json
{
  "type": "metadata",
  "agent": "AgentName",
  "event": "EventType",
  "data": {
    "currentAgentName": "AgentName",
    "message": "...",
    "streaming": true
  }
}
```

**Example with curl:**
```bash
curl -N -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Plan a trip to Paris","context":{}}'
```

## Architecture

### Simplified MCP Integration

This implementation uses **Microsoft Agent Framework's built-in MCP support**, removing all custom implementations:

- ✅ `MCPStreamableHTTPTool` from MAF (no custom transport)
- ✅ Factory pattern for tool creation (no persistent connections)
- ✅ MAF handles all lifecycle management (no manual async context managers)
- ✅ Explicit errors for missing or unsuccessful planned tool work

The customer and itinerary agents receive tools from the registry; the destination agent uses the LLM without MCP.

### Magentic Orchestration

Uses MAF's Magentic pattern for multi-agent coordination:

The workflow coordinates customer, itinerary, and destination agents through a manager agent. Its planning call
produces a structured specialist and tool plan, and the manager verifies that work before authorizing a final answer.
Invalid plans and unverified round or reset termination produce errors. Greetings can use a direct response. Intent
selection remains model-driven, so an incorrectly classified plan can still omit needed work.

For a planned tool, failed or missing calls remain pending until a later call with the same normalized inputs succeeds,
including after a workflow reset. Success for different inputs does not clear them, so a retry that corrects inputs may
still end in an error.

When a provider reuses a tool-call ID, each occurrence needs a distinct SDK occurrence ID and a matching result ID.
Providers or SDK paths that omit those IDs fail closed on reuse; non-reused calls do not require them.

### Agent Architecture

```
┌─────────────────────────────────────────┐
│      FastAPI Application (main.py)     │
│  /api/health  /api/tools  /api/chat    │
└───────────────────┬─────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│   MagenticTravelOrchestrator            │
│   (magentic_workflow.py)                │
│                                         │
│   • Magentic workflow builder           │
│   • Event streaming                     │
│   • Multi-agent coordination            │
└────────────┬────────────────────────────┘
             │
    ┌────────┼────────┐
    │        │        │
    ▼        ▼        ▼
┌─────────┐ ┌─────────┐ ┌─────────┐
│Customer │ │Itinerary│ │Destin.  │
│Query    │ │ Agent   │ │ Agent   │
│Agent    │ │         │ │         │
└────┬────┘ └────┬────┘ └─────────┘
     │           │
     ▼           ▼
┌─────────────────────────────────┐
│  MCPStreamableHTTPTool (MAF)    │
│                                 │
│  • Created per-agent            │
│  • Lifecycle managed by MAF     │
│  • Connects to MCP servers      │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│     MCP Servers (Docker)        │
│                                 │
│  • customer-query (5001/mcp)    │
│  • itinerary-planning (5003/mcp/)│
│  • echo-ping (5004/mcp)         │
└─────────────────────────────────┘
```

### Additional Resources

- [Microsoft Agent Framework Docs](https://learn.microsoft.com/en-us/agent-framework/)
- [Magentic Orchestration](https://learn.microsoft.com/en-us/agent-framework/user-guide/workflows/orchestrations/magentic?pivots=programming-language-python)
- [MCP Tools in MAF](https://learn.microsoft.com/en-us/agent-framework/user-guide/model-context-protocol/using-mcp-tools)
- [MAF Python Samples](https://github.com/microsoft/agent-framework/tree/main/python/samples)

## Development

### LLM Provider Configuration

Choose your LLM provider in `.env`:

```txt
LLM_PROVIDER=azure-openai  # or github-models, ollama-models, docker-models
```

**Azure OpenAI:**
```txt
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-api-key
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-5
```

**GitHub Models:**
```txt
GITHUB_TOKEN=your-github-token
GITHUB_MODEL=openai/gpt-5o
```

**Ollama:**
```txt
OLLAMA_ENDPOINT=http://localhost:11434/v1
OLLAMA_MODEL=llama3
```

### Code Quality

```bash
# Linting
uvx ruff check src/

# Auto-fix
uvx ruff check --fix src/

# Format
uvx ruff format src/

# Type checking
uvx mypy src/
```

### Testing

```bash
# Run tests
pytest

# With coverage
pytest --cov=src --cov-report=html
```


## Troubleshooting

### MCP Server Connection Issues

**Problem:** MCP servers not reachable

**Solutions:**
1. Check if Docker containers are running: `docker ps`
2. Verify URLs in `.env` match container ports
3. Test manually: `curl http://localhost:5001/mcp`
4. Check container logs: `docker logs <container-name>`

**Problem:** Cancel scope violations or async errors

**Solution:** Already fixed! The simplified architecture uses MAF's built-in lifecycle management. Each agent gets its own MCP tool instance, avoiding shared async context managers.

### Event Streaming Issues

**Problem:** UI not receiving events

**Checklist:**
1. ✅ Using `workflow.run(..., stream=True)`
2. ✅ Events converted in `_convert_workflow_event()`
3. ✅ SSE headers set correctly
4. ✅ No buffering in nginx/proxy

### Agent Not Using Tools

**Problem:** Agent doesn't call MCP tools

**Check:**
1. Tool passed to the agent (not `None`)
2. MCP server is actually reachable
3. LLM model supports function calling
4. Instructions mention using tools

### Common Errors

**Error:** `'ToolRegistry' object has no attribute 'list_tools'`  
**Fix:** Already fixed - `list_tools()` method is now async and implemented correctly.

**Error:** `Attempted to exit cancel scope in a different task`  
**Fix:** Already fixed - using factory pattern, no shared MCP tools across tasks.

**Error:** Connection refused  
**Fix:** Ensure MCP servers are running via `./run.sh` from repo root.

## Key Features

### Tool Availability

Tools listed as required in the manager's plan must succeed before a final answer. A tool-optional request can use
LLM-only behavior when tool configuration is absent; a configured but unreachable server can still fail. The
model-selected plan can omit work the user requested.

### Real-time Streaming

Intermediate and final workflow output streams to the UI.

### Built-in Observability

OpenTelemetry exports to Azure Monitor when an Application Insights connection string is configured, or to the configured OTLP endpoint otherwise.

## Documentation

- **[Microsoft Agent Framework Docs](https://learn.microsoft.com/en-us/agent-framework/)** - Official MAF documentation
- **[MAF Python Samples](https://github.com/microsoft/agent-framework/tree/main/python/samples)** - Official code samples

## Troubleshooting

### Common Issues

**"Cancel scope in different task" error**
- Fixed by using MAF's built-in tool lifecycle management
- Don't manage `async with tool:` manually

**MCP server not reachable**
- Check server is running: `curl http://localhost:5001/health`
- Work planned to require that server's tool cannot complete successfully until it is available.

**Events not streaming**
- Ensure using `workflow.run(..., stream=True)`
- Check asyncio.Queue is properly configured

### Testing

```bash
# Health check
curl http://localhost:4010/api/health

# List tools
curl http://localhost:4010/api/tools

# Test chat with streaming
curl -N -X POST http://localhost:4010/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Plan a 3-day trip to Paris"}' \
  --no-buffer
```

## License

Same as the main Azure AI Travel Agents project.
