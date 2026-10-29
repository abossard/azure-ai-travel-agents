import { BaseChatModel } from "@langchain/core/language_models/chat_models";
import { BaseMessage, HumanMessage } from "@langchain/core/messages";
import { McpServerDefinition } from "../utils/types.js";
import { AgentState, setupAgents } from "../agents/index.js";
import { ReactAgent } from "langchain";

export interface WorkflowState extends AgentState {
  messages: BaseMessage[];
  currentAgent?: string;
  toolsOutput?: any[];
  next?: string;
}

const DEBUG_CONTENT_PREVIEW_LENGTH = 1024;

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function previewDebugContent(value: unknown): unknown {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (!text || text.length <= DEBUG_CONTENT_PREVIEW_LENGTH) return value;

  let preview = "";
  let length = 0;
  for (const character of text) {
    if (length++ === DEBUG_CONTENT_PREVIEW_LENGTH) return `${preview}...`;
    preview += character;
  }
  return value;
}

function projectMessages(value: unknown, includeTools: boolean) {
  if (!Array.isArray(value)) return undefined;

  // The reasoning panel reads the last message's content and the second message's tool names.
  return value.map((message, index) => {
    const record = asRecord(message);
    const fields = asRecord(record?.kwargs) ?? record;
    const metadata = asRecord(fields?.response_metadata);
    const tools = includeTools && index === 1 && Array.isArray(metadata?.tools)
      ? metadata.tools.map((tool: unknown) => ({ name: asRecord(tool)?.name }))
      : undefined;

    return {
      kwargs: {
        ...(index === value.length - 1 && fields?.content !== undefined && {
          content: previewDebugContent(fields.content),
        }),
        ...(includeTools && index === 1 && tools && { response_metadata: { tools } }),
      },
    };
  });
}

function projectDebugData(agent: string, value: unknown) {
  const data = asRecord(value);
  const inputMessages = projectMessages(asRecord(data?.input)?.messages, false);
  const output = asRecord(data?.output);
  const outputMessages = projectMessages(asRecord(output?.update)?.messages, true);

  return {
    agent: typeof data?.agent === "string" ? data.agent : agent,
    ...(inputMessages && { input: { messages: inputMessages } }),
    ...(output && (output.goto !== undefined || outputMessages) && {
      output: {
        ...(output.goto !== undefined && { goto: output.goto }),
        ...(outputMessages && { update: { messages: outputMessages } }),
      },
    }),
  };
}

export class TravelAgentsWorkflow {
  private llm: BaseChatModel;
  private agents: any[] = [];
  private deepAgent: ReactAgent | null = null;

  constructor(llm: BaseChatModel) {
    this.llm = llm;
  }

  async initialize(filteredTools: McpServerDefinition[] = []) {
    console.log("Initializing Langchain workflow with filtered tools:", filteredTools.map(t => t.id));
    
    // Setup agents and tools
    const { agents, deepAgent} = await setupAgents(filteredTools, this.llm);
    this.agents = agents;
    this.deepAgent = deepAgent;

    console.log("Langchain workflow initialized successfully");
    console.log("Available agents:", Object.keys(this.agents));
  }

  async *run(input: string) {
    if (Object.keys(this.agents).length === 0) {
      throw new Error("Workflow not initialized. Call initialize() first.");
    }

    const agent = "TravelAgent";
    const messages = [new HumanMessage(input)];

    try {

      const eventStream = this.deepAgent!.streamEvents({
        messages,
      });

      for await (const event of eventStream) {
        // Filter and yield relevant events
        if (event.event === "on_chat_model_stream") {
          // Stream LLM tokens
          const chunk = asRecord(event.data?.chunk);
          const content = chunk?.content ?? asRecord(chunk?.kwargs)?.content;
          yield {
            eventName: "llm_token",
            data: {
              agent,
              chunk: chunk && { kwargs: { content } },
            },
          };
        } else if (event.event === "on_tool_start") {
          // Tool execution started
          yield {
            eventName: "tool_start",
            data: {
              agent,
              tool: event.name,
              input: event.data?.input,
            },
          };
        } else if (event.event === "on_tool_end") {
          // Tool execution completed
          yield {
            eventName: "tool_end",
            data: {
              agent,
              tool: event.name,
              output: event.data?.output,
            },
          };
        } else if (event.event === "on_chain_end" && event.name === "LangGraph") {
          // Agent completed - yield final response
          yield {
            eventName: "agent_complete",
            data: {
              agent,
              messages: event.data?.output?.messages || [],
            },
          };
        }
        else {
          // Yield other events for debugging
          yield {
            eventName: event.event,
            data: projectDebugData(agent, event.data),
          };
        }
      }

    } catch (error) {
      console.error("Error in workflow execution:", error);
      yield {
        eventName: "workflow_error",
        data: {
          agent,
          error: error instanceof Error ? error.message : String(error),
        },
      };
    }
  }
}