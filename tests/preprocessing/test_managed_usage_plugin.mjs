import assert from "node:assert/strict"
import { test } from "node:test"
import plugin from "../../preprocessing/managed_usage_plugin/index.js"

test("bundled extraction hooks leave shared-engine chat requests and retry policy unchanged", async () => {
	const hooks = new Map()
	await plugin.setup({ session: {
		get: async ({ sessionID }) => ({ id: sessionID, agent: "geology", metadata: {} }),
		hook: async (name, handler) => hooks.set(name, handler),
	} })
	const request = new Request("http://managed.test/chat/completions", {
		method: "POST", body: "{}", headers: { "x-geocluster-request-id": "owned-by-chat-runtime" },
	})
	const before = { sessionID: "ses_chat", agent: "geology", kind: "primary", request }
	await hooks.get("http.request")(before)
	assert.equal(before.request, request)
	assert.equal(before.request.headers.get("x-geocluster-request-id"), "owned-by-chat-runtime")
	const response = new Response(JSON.stringify({ error: { code: "usage_unavailable", retryable: false } }), { status: 503 })
	const after = { ...before, response }
	await hooks.get("http.response")(after)
	assert.equal(after.response, response)
	const decision = { retry: true, delay: 123 }
	const retry = { sessionID: "ses_chat", agent: "geology", error: { status: 503 }, attempt: 2, decision }
	await hooks.get("retry")(retry)
	assert.equal(retry.decision, decision)
})
