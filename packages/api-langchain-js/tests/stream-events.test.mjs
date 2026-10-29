import assert from "node:assert/strict";
import test from "node:test";
import { BaseChatModel } from "@langchain/core/language_models/chat_models";
import { AIMessage, AIMessageChunk, HumanMessage } from "@langchain/core/messages";
import { ChatGenerationChunk } from "@langchain/core/outputs";
import { tool } from "@langchain/core/tools";
import { createDeepAgent } from "deepagents";
import { z } from "zod";
import { TravelAgentsWorkflow } from "../src/graph/index.ts";

class FixtureModel extends BaseChatModel {
  constructor() {
    super({});
  }

  _llmType() {
    return "fixture";
  }

  bindTools() {
    return this;
  }

  messageFor(messages) {
    const calls = messages.filter((message) => message._getType() === "tool").length;
    if (calls < 2) {
      return new AIMessage({
        content: "",
        tool_calls: [{ name: "get_destinations", args: { query: "fixture" }, id: `call-${calls}`, type: "tool_call" }],
      });
    }
    return new AIMessage({ content: [{ type: "text", text: "A useful final answer." }] });
  }

  async _generate(messages) {
    return { generations: [{ text: "", message: this.messageFor(messages) }] };
  }

  async *_streamResponseChunks(messages, _options, runManager) {
    const response = this.messageFor(messages);
    if (response.tool_calls?.length) {
      const call = response.tool_calls[0];
      const chunk = new ChatGenerationChunk({
        text: "",
        message: new AIMessageChunk({
          content: "",
          tool_call_chunks: [{ name: call.name, id: call.id, args: JSON.stringify(call.args), index: 0 }],
        }),
      });
      yield chunk;
      await runManager?.handleLLMNewToken("", undefined, undefined, undefined, undefined, { chunk });
      return;
    }
    for (const word of ["A ", "useful ", "final ", "answer."]) {
      const chunk = new ChatGenerationChunk({
        text: word,
        message: new AIMessageChunk({ content: [{ type: "text", text: word }] }),
      });
      yield chunk;
      await runManager?.handleLLMNewToken(word, undefined, undefined, undefined, undefined, { chunk });
    }
  }
}

for (const resultSize of [200, 50_000]) {
  test(`SDK events preserve tool results and answer under 2 MiB (${resultSize} bytes)`, async () => {
    const toolResult = JSON.stringify({ destinations: "x".repeat(resultSize) });
    const destination = tool(
      async () => toolResult,
      { name: "get_destinations", description: "Find destinations", schema: z.object({ query: z.string() }) },
    );
    const model = new FixtureModel();
    const deepAgent = createDeepAgent({ model, tools: [destination], subagents: [], systemPrompt: "Plan a trip." });
    const nativeEvents = [];
    for await (const event of deepAgent.streamEvents({ messages: [new HumanMessage("Plan a trip.")] })) {
      nativeEvents.push(event);
    }

    const workflow = new TravelAgentsWorkflow(model);
    Reflect.set(workflow, "agents", [{}]);
    Reflect.set(workflow, "deepAgent", { streamEvents: async function* () { yield* nativeEvents; } });

    const events = [];
    let bytes = 0;
    for await (const event of workflow.run("Plan a trip.")) {
      const serialized = JSON.stringify({
        type: "metadata",
        kind: "langchain-js",
        agent: event.data?.agent ?? null,
        event: event.eventName,
        data: event.data,
      }) + "\n\n";
      bytes += Buffer.byteLength(serialized);
      events.push(JSON.parse(serialized));
    }

    assert.ok(bytes < 2 * 1024 * 1024, `stream used ${bytes} bytes`);
    const toolStarts = events.filter((event) => event.event === "tool_start");
    const toolEnds = events.filter((event) => event.event === "tool_end");
    assert.equal(toolStarts.length, 2);
    assert.equal(toolEnds.length, 2);
    for (const event of toolStarts) {
      assert.equal(event.data.tool, "get_destinations");
      assert.deepEqual(JSON.parse(event.data.input.input), { query: "fixture" });
    }
    for (const event of toolEnds) {
      assert.equal(event.data.tool, "get_destinations");
      assert.equal(event.data.output.kwargs.content, toolResult);
    }
    const answer = events
      .filter((event) => event.event === "llm_token")
      .flatMap((event) => event.data.chunk.kwargs.content || [])
      .map((block) => block.text ?? "")
      .join("");
    assert.equal(answer, "A useful final answer.");
    const final = events.find((event) => event.event === "agent_complete");
    const nativeFinal = nativeEvents.find((event) => event.event === "on_chain_end" && event.name === "LangGraph");
    assert.deepEqual(final.data.messages, JSON.parse(JSON.stringify(nativeFinal.data.output.messages)));
    assert.equal(final.data.messages.at(-1).kwargs.content.map((block) => block.text).join(""), answer);
    assert.ok(events.some((event) => event.event === "on_chain_start"));
    assert.ok(events.some((event) => event.event === "on_chain_end"));

    for (const [index, native] of nativeEvents.entries()) {
      if (native.event !== "on_chain_start" && (native.event !== "on_chain_end" || native.name === "LangGraph")) continue;
      const input = JSON.parse(JSON.stringify(native.data)).input?.messages;
      if (!Array.isArray(input) || input.length === 0) continue;
      const original = input.at(-1)?.kwargs?.content;
      const projected = events[index].data.input.messages.at(-1)?.kwargs?.content;
      const text = typeof original === "string" ? original : JSON.stringify(original);
      if (text && Array.from(text).length > 1024) {
        assert.ok(projected.endsWith("..."));
        assert.ok(text.startsWith(projected.slice(0, -3)));
      } else {
        assert.deepEqual(projected, original);
      }
    }
  });
}

for (const [label, content, truncated] of [
  ["short text", "visible content", false],
  ["long text", "a".repeat(2000), true],
  ["Unicode text", "🌍".repeat(1100), true],
  ["structured content", [{ type: "text", text: "b".repeat(2000) }], true],
]) {
  test(`debug event preserves the UI fields with ${label}`, async () => {
    const input = { kwargs: { content } };
    const output = {
      goto: "next",
      update: {
        messages: [
          { kwargs: { content: "previous" } },
          { kwargs: { content, response_metadata: { tools: [{ name: "destination", schema: "x".repeat(5000) }] } } },
        ],
      },
    };
    const events = [
      { event: "on_chain_start", data: { input: { messages: [input] } } },
      { event: "on_chain_end", name: "node", data: { output } },
    ];
    const workflow = new TravelAgentsWorkflow(new FixtureModel());
    Reflect.set(workflow, "agents", [{}]);
    Reflect.set(workflow, "deepAgent", { streamEvents: async function* () { yield* events; } });
    const result = [];
    for await (const event of workflow.run("fixture")) result.push(event);

    const inputContent = result[0].data.input.messages.at(-1).kwargs.content;
    const outputContent = result[1].data.output.update.messages.at(-1).kwargs.content;
    if (truncated) {
      assert.equal(typeof inputContent, "string");
      assert.ok(inputContent.endsWith("..."));
      assert.equal(Array.from(inputContent).length, 1027);
      assert.equal(outputContent, inputContent);
    } else {
      assert.deepEqual(inputContent, content);
      assert.deepEqual(outputContent, content);
    }
    assert.equal(result[1].data.output.goto, "next");
    assert.deepEqual(result[1].data.output.update.messages.at(1).kwargs.response_metadata.tools, [{ name: "destination" }]);
  });
}

for (const [label, failure, message] of [
  ["error", new Error("upstream failure"), "upstream failure"],
  ["non-error", "upstream failure", "upstream failure"],
]) {
  test(`workflow reports a streamed ${label}`, async () => {
    const workflow = new TravelAgentsWorkflow(new FixtureModel());
    Reflect.set(workflow, "agents", [{}]);
    Reflect.set(workflow, "deepAgent", {
      streamEvents: async function* () { throw failure; },
    });
    const originalError = console.error;
    const logs = [];
    console.error = (...args) => logs.push(args);
    try {
      const events = [];
      for await (const event of workflow.run("fixture")) events.push(event);
      assert.equal(events.length, 1);
      assert.equal(events[0].eventName, "workflow_error");
      assert.equal(events[0].data.error, message);
      assert.equal(logs.length, 1);
    } finally {
      console.error = originalError;
    }
  });
}
