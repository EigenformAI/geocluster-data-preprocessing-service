// Bundled extraction-only hook. The owner supplies structured session metadata and the proxy credential.
import { randomUUID } from "node:crypto"

const DENIALS = {
	usage_limit_reached: "Your model allowance is unavailable for this period.",
	model_not_allowed: "This model or request path is not available for this account.",
	funding_ended: "Funding for this account has ended.",
	request_already_admitted: "This model request was already admitted.",
	usage_unavailable: "Usage could not be checked. New model requests are paused.",
}
const message = (code) => `[geocluster:${code}] ${DENIALS[code]} Submit an explicit new job after managed access is restored.`
function denial(value, depth = 0) {
	if (depth > 8 || value == null) return null
	if (typeof value === "string") {
		const code = value.toLowerCase()
		if (Object.hasOwn(DENIALS, code)) return code
		const marker = /\[geocluster:([a-z_]+)\]/.exec(value)
		if (marker && Object.hasOwn(DENIALS, marker[1])) return marker[1]
		const start = value.indexOf("{"), end = value.lastIndexOf("}")
		if (start >= 0 && end > start) { try { return denial(JSON.parse(value.slice(start, end + 1)), depth + 1) } catch {} }
	} else if (typeof value === "object") {
		for (const key of ["code", "type", "error", "message", "data", "cause"]) {
			const found = denial(value[key], depth + 1)
			if (found) return found
		}
	}
	return null
}

export default {
	id: "preprocessing-managed-usage",
	async setup(ctx) {
		const deniedJobs = new Map()
		async function binding(sessionID) {
			const info = await ctx.session.get({ sessionID })
			const meta = info.metadata?.preprocessing
			if (info.agent !== "preprocessing" && !meta) return null
			if (info.parentID || info.agent !== "preprocessing" || !meta ||
				![meta.jobId, meta.operationId, meta.inputId, info.id].every((id) => typeof id === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(id))) {
				throw new Error(message("usage_unavailable"))
			}
			return { job: meta.jobId, root: info.id }
		}
		await ctx.session.hook("http.request", async (e) => {
			const owner = await binding(e.sessionID)
			if (!owner) return
			const { job, root } = owner
			if (deniedJobs.has(job)) throw new Error(message(deniedJobs.get(job)))
			const headers = new Headers(e.request.headers)
			for (const key of [...headers.keys()]) if (key.startsWith("x-geocluster-")) headers.delete(key)
			for (const [key, value] of Object.entries({ "request-id": randomUUID(), "work-id": job,
				"conversation-id": root, agent: "preprocessing", kind: "preprocessing" })) headers.set(`x-geocluster-${key}`, value)
			e.request = new Request(e.request, { headers })
		})
		await ctx.session.hook("http.response", async (e) => {
			if (e.response.ok) return
			const owner = await binding(e.sessionID)
			if (!owner) return
			let value
			const copy = e.response.clone(), reader = copy.body?.getReader(), chunks = []
			let size = 0
			try {
				if (reader) for (;;) {
					const { value: chunk, done } = await reader.read()
					if (done) break
					size += chunk.byteLength
					if (size > 65536) break
					chunks.push(chunk)
				}
				if (size <= 65536) value = JSON.parse(Buffer.concat(chunks).toString("utf8"))
			} catch {} finally { void reader?.cancel().catch(() => {}) }
			const code = denial(value) || ({ 401: "usage_unavailable", 402: "usage_limit_reached", 403: "model_not_allowed", 409: "request_already_admitted" })[e.response.status] ||
				(e.response.status === 503 && value?.error?.retryable === false ? "usage_unavailable" : null)
			if (!code) return
			deniedJobs.set(owner.job, code)
			const headers = new Headers(e.response.headers)
			headers.delete("content-length"); headers.delete("content-encoding"); headers.set("content-type", "application/json")
			e.response = new Response(JSON.stringify({ error: { code, message: message(code), retryable: false } }), { status: e.response.status, headers })
		})
		await ctx.session.hook("retry", async (e) => {
			let owner
			try { owner = await binding(e.sessionID) } catch { e.decision = { retry: false }; return }
			if (!owner) return
			const code = denial(e.error) || deniedJobs.get(owner.job)
			if (code || [401, 402, 403, 409].includes(Number(e.error?.status)) || e.decision?.retry === false || e.attempt > 4) {
				e.decision = { retry: false }
			} else {
				e.decision = { retry: true, delay: Math.min(30000, Math.max(e.decision?.delay || 0, 2000 * 2 ** Math.max(0, e.attempt - 2))) }
			}
		})
	},
}
