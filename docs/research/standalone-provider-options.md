# Standalone provider options for OpenCode 2.0.19

Research and verification date: 2026-10-01. This note distinguishes official
provider guidance from behavior observed through the pinned server.

## Preferred Luna model

Official OpenAI documentation identifies **`gpt-6-luna`**, accepting text and
images and producing text. It supports the Responses endpoint. Documentation
establishes capabilities, not this account's access or subscription entitlement.

For API-backed extraction, a controlled provider can declare:

```json
{
	"model": "openai/gpt-6-luna",
	"providers": {
		"openai": {
			"package": "@opencode/ai/providers/openai/responses",
			"settings": {
				"baseURL": "https://api.openai.com/v1",
				"apiKey": "{env:PREPROCESSING_PROVIDER_TOKEN}"
			},
			"models": {
				"gpt-6-luna": {
					"name": "GPT-6 Luna",
					"capabilities": {"tools": false, "input": ["text", "image"], "output": ["text"]},
					"limit": {"context": 1050000, "output": 128000}
				}
			}
		}
	}
}
```

This is only the provider fragment. Retain the extraction-only agent and other
controlled fields from the complete example configuration. Forward the named
credential explicitly with `--credential-env PREPROCESSING_PROVIDER_TOKEN`.
No live Luna request was made: an approved API key is not provisioned.

ChatGPT subscription authentication is distinct from a Platform API key. OpenCode
documents browser/headless subscription login and saved server accounts. The
current isolated owner accepts only explicit environment-interpolated provider
credentials; it does not provision or import subscription accounts. Subscription
testing therefore needs an explicit isolated account workflow. An anonymous
placeholder is not a subscription credential.

## Approved free fallback

The public Zen catalog listed **`muse-spark-1.3-contributor-free`**. Official Zen
guidance places it on `https://opencode.ai/zen/v1/responses`.

An actual 2.0.19 owner selected the native Responses provider and used
`STANDALONE_FREE_PROBE_TOKEN=anonymous` in a private extraction context. The
existing service-bound probe reached the provider and received:

```json
{"type":"provider.auth","message":"Invalid API key.","status":401}
```

The failed text prerequisite prevented image/PDF attempts. One failed operation
was durably recorded and locally settled, with the owned session deleted. Owner
shutdown returned 0 and withdrew admission. This proves that the named anonymous
credential is rejected; it does not prove whether an omitted authorization header
would work. The current controlled launcher requires a nonempty named credential.

DeepSeek variants need confirmed image support before use for OCR. No DeepSeek
or paid fallback request was made. Catalog presence and declared capability are
not inference-quality evidence.

## Pin and packaging facts

- The authoritative npm distribution is **`@opencode/cli@2.0.19`**.
- The optional image downloads `@opencode/cli-linux-x64-baseline@2.0.19` for
  amd64 or `@opencode/cli-linux-arm64@2.0.19` for arm64. It copies only the native
  executable into the Python image; Node/npm remain in the build stage.
- The actual amd64 image launched and verified `/api/info` version 2.0.19.
- `@ai-sdk/openai-compatible`, used by the existing example, is accepted at this
  pin: completed real-server tests and container image turns exercised it with
  a deterministic loopback provider.
- The native `@opencode/ai/providers/openai/responses` package reached the real
  Zen authentication failure above.
- The isolated owner uses controlled HOME/XDG roots, one canonical extraction
  directory, an explicit catalog, and only named credential variables. Personal
  authentication stores and secret process environments were not inspected.

## Sources and retained evidence

- [GPT-6 Luna model](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [OpenAI authentication](https://developers.openai.com/codex/auth/)
- [OpenCode v2 providers](https://opencode.ai/v2/docs/providers)
- [OpenCode v2 provider authentication](https://opencode.ai/v2/docs/cli/providers)
- [OpenCode Zen endpoints](https://opencode.ai/docs/zen/)
- [Public Zen model catalog](https://opencode.ai/zen/v1/models)
- [Pinned amd64 npm binary](https://www.npmjs.com/package/@opencode/cli-linux-x64-baseline/v/2.0.19)

The redacted provider rejection, selected model, commands, and ledger outcomes
are retained under `/tmp/opencode/muse-free-verification-CoJn8hSX/`. Full standalone
acceptance and memory observations are recorded separately in
`docs/research/standalone-acceptance.md`.
