# JSON-mode Provider Compatibility

## Status

Approved in conversation on 2026-08-06. This specification covers only the
structured-output compatibility failure that blocks the existing short-book run.

## Problem

ABI sends Planner requests through `LLMRouter.invoke_structured()`, which binds a
Pydantic schema with LangChain `json_mode`. The configured OpenAI-compatible
endpoint rejects `response_format=json_object` unless at least one prompt message
explicitly contains the word `json`. The current Planner prompt does not, so the
first planning call returns HTTP 400 before a plan or Action is durably created.

The durable run remains recoverable at plan version zero. The existing run must be
resumed after the compatibility fix rather than replaced.

## Decision

The provider boundary, not individual business prompts, owns this transport
compatibility rule. `LLMRouter` will ensure every call made through
`invoke_structured()` contains a stable system instruction requiring a JSON object
that conforms to the requested schema.

The instruction must be added before prompt hashing and invocation so observability
records the exact effective prompt. Existing business messages, schema validation,
retry behavior, model selection, budgeting, and durable orchestration semantics stay
unchanged.

This fix does not change `AgentRuntime` structured-response behavior because the
observed failure is on the `LLMRouter` JSON-mode path. If the subsequent real run
reveals a distinct AgentRuntime compatibility failure, it will be diagnosed and
handled separately.

## Alternatives Rejected

1. Add `json` only to the Planner prompt. This repairs the first call but leaves
   every other `invoke_structured()` caller dependent on incidental prompt wording.
2. Switch globally to function calling or JSON Schema. Endpoint support has not been
   established, and the change would be broader than the observed defect.

## Test Strategy

Development follows RED-GREEN:

1. Add a focused provider test whose business messages contain no `json` token and
   assert that the effective messages passed to the structured model contain the
   provider-owned JSON instruction.
2. Run the test before production changes and confirm it fails for the missing
   instruction.
3. Add the smallest provider-boundary implementation and confirm the focused test,
   LLM router tests, and Planner tests pass.
4. Run the relevant regression suite and repository verification appropriate to the
   changed scope.
5. Resume the existing short-book project and run `inspect`, L1 trace, and three-plane
   book eval. Success means the original HTTP 400 no longer occurs; completion of the
   whole book remains subject to any independently discovered downstream issue.

## Out of Scope

- Langfuse integration import failure;
- durable classification of provider HTTP errors;
- accounting of failed provider calls;
- pricing configuration for `deepseek-v4-flash`;
- changes to Planner authority, policy, scheduling, or artifact protocols.
