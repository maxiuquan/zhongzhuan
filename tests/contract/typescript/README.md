# T37 TypeScript SDK contract tests (R-P1-49)

Official `openai` Node SDK contract tests against a localhost mock (zero real
network).  Run with:

```bash
npm install            # once, keeps node_modules under tests/contract/typescript
npm test               # tsc build + node --test dist/test/
```

## Coverage vs the four T37 criteria

| Criterion | TypeScript | Python (tests/contract/python) |
|---|---|---|
| ① six operations (create/retrieve/delete/cancel/compact/input_items), zero vendor-specific code | create / retrieve / del / cancel / input_items via the official SDK; `compact` is **not exposed by openai-node 4.104.0** (the test asserts the absence so a future SDK bump surfaces it) | full six via openai 2.53.0 |
| ② store / previous_response / background multi-turn | equivalent API surface is homogeneous with Python; covered on the Python side | full |
| ③ full streaming event schema | full official event sequence + monotonic sequence_number + delta text + completed terminal state | full |
| ④ differential test vs native OpenAI | equivalent API surface is homogeneous with Python; covered on the Python side | full (mock-native vs zhongzhuan relay) |

## SDK versions

* openai (Node): **4.104.0** — `Responses` exposes `create` / `retrieve` /
  `del` (not `delete`) / `cancel` (void) / `parse` / `stream` / `inputItems`.
  `compact` is not part of the resource in this version.
* openai (Python): **2.53.0** — full six operations including `compact`
  (501 honest stub on the v3 skeleton).

## Test scaffolding

`src/mock-server.ts` is a plain `node:http` server implementing the six
`/v1/responses` resource endpoints.  It exists so the SDK can be exercised
against a localhost endpoint; it is NOT the code under test and is not
SDK-specific.
