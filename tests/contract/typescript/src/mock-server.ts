/**
 * T37 mock server (test scaffolding only, NOT the code under test).
 *
 * A plain `node:http` server that implements the six `/v1/responses` resource
 * endpoints the official OpenAI SDK talks to.  This is the "zhongzhuan /
 * native" stand-in for the TypeScript contract tests -- it lets the SDK
 * exercise its official calls against a localhost endpoint with zero real
 * network, mirroring the shape the Python side verifies against the real
 * `ResponsesV3Handler`.
 *
 * The contract assertions live in `src/test/*.test.ts` and only ever call the
 * official SDK (no hand-rolled HTTP, no private SDK fields) -- that is what
 * criterion ① "zero vendor-specific code" means for the test side.
 */

import { createServer, IncomingMessage, Server, ServerResponse } from 'node:http';
import { AddressInfo } from 'node:net';

/** Official minimal Responses streaming event set (mirrors
 *  `tests/support/mock_responses_upstream.py:responses_text_stream`). */
export const RESPONSE_STREAM_EVENTS: string[] = [
  'response.created',
  'response.in_progress',
  'response.output_item.added',
  'response.content_part.added',
  'response.output_text.delta',
  'response.output_text.delta',
  'response.output_text.delta',
  'response.output_text.delta',
  'response.output_text.done',
  'response.content_part.done',
  'response.output_item.done',
  'response.completed',
];

export interface MockServer {
  server: Server;
  url: string; // http://127.0.0.1:<port>/v1
  requests: Array<{ method: string; path: string; body: string }>;
  close: () => Promise<void>;
}

const RESP_ID = 'resp_fixture000000000001';
const ITEM_ID = 'msg_fixture_item_0';
const CREATED_AT = 1700000000;

function jsonResponse(res: ServerResponse, status: number, obj: unknown): void {
  const payload = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) });
  res.end(payload);
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (c: Buffer) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

/** Build a response object matching `zhongzhuan.responses_v3.schema.to_response_object`. */
function responseObject(rid: string, status: string): Record<string, unknown> {
  return {
    id: rid,
    object: 'response',
    created_at: CREATED_AT,
    model: 'gpt-4o',
    status,
    output: [],
    usage: { input_tokens: 3, output_tokens: 1, total_tokens: 4 },
    error: null,
    incomplete_details: null,
    instructions: null,
    metadata: {},
    previous_response_id: null,
    background: false,
    tools: [],
    tool_choice: 'auto',
    parallel_tool_calls: true,
    temperature: null,
    top_p: null,
    max_output_tokens: null,
    text: null,
    truncation: null,
    user: null,
    store: true,
    include: [],
    stream: false,
  };
}

/** Deterministic native Responses SSE payload (mirrors `responses_text_stream`). */
function responsesSse(): string {
  const base: Record<string, unknown> = {
    id: RESP_ID,
    object: 'response',
    created_at: CREATED_AT,
    model: 'upstream-model',
    status: 'in_progress',
    output: [],
  };
  const fullText = 'Hello, world!';
  const frames: string[] = [];
  let seq = 0;
  const emit = (event: string, payload: Record<string, unknown>): void => {
    frames.push(`event: ${event}\ndata: ${JSON.stringify({ type: event, sequence_number: seq++, ...payload })}\n\n`);
  };
  emit('response.created', { response: { ...base } });
  emit('response.in_progress', { response: { ...base } });
  emit('response.output_item.added', {
    output_index: 0,
    item: { id: ITEM_ID, type: 'message', status: 'in_progress', role: 'assistant', content: [] },
  });
  emit('response.content_part.added', {
    item_id: ITEM_ID,
    output_index: 0,
    content_index: 0,
    part: { type: 'output_text', text: '', annotations: [] },
  });
  for (const piece of ['Hello', ', ', 'world', '!']) {
    emit('response.output_text.delta', { item_id: ITEM_ID, output_index: 0, content_index: 0, delta: piece });
  }
  emit('response.output_text.done', { item_id: ITEM_ID, output_index: 0, content_index: 0, text: fullText });
  emit('response.content_part.done', {
    item_id: ITEM_ID,
    output_index: 0,
    content_index: 0,
    part: { type: 'output_text', text: fullText, annotations: [] },
  });
  emit('response.output_item.done', {
    output_index: 0,
    item: {
      id: ITEM_ID,
      type: 'message',
      status: 'completed',
      role: 'assistant',
      content: [{ type: 'output_text', text: fullText, annotations: [] }],
    },
  });
  const completed = { ...base, status: 'completed', usage: { input_tokens: 11, output_tokens: 4, total_tokens: 15 } };
  emit('response.completed', { response: completed });
  frames.push('data: [DONE]\n\n');
  return frames.join('');
}

/** Start the mock server on a random localhost port. */
export async function startMockServer(): Promise<MockServer> {
  const requests: Array<{ method: string; path: string; body: string }> = [];
  /** IDs created by this isolated server instance. */
  const knownIds = new Set<string>([RESP_ID]);

  const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1');
    const path = url.pathname;
    const method = req.method || 'GET';
    const body = await readBody(req);
    requests.push({ method, path, body });

    // POST /v1/responses -> create (stream or non-stream)
    if (method === 'POST' && path === '/v1/responses') {
      const parsed = JSON.parse(body || '{}');
      if (parsed.stream === true) {
        const sse = responsesSse();
        res.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          'Content-Length': Buffer.byteLength(sse),
        });
        res.end(sse);
        return;
      }
      const rid = 'resp_' + Math.random().toString(16).slice(2);
      knownIds.add(rid);
      jsonResponse(res, 200, responseObject(rid, 'in_progress'));
      return;
    }

    // POST /v1/responses/compact -> 501 honest stub
    if (method === 'POST' && path === '/v1/responses/compact') {
      jsonResponse(res, 501, {
        error: { message: 'compact is not implemented in the v3 skeleton (T24/T28)', type: 'not_implemented', code: 'not_implemented' },
      });
      return;
    }

    // /v1/responses/{id}[/cancel | /input_items]
    const m = path.match(/^\/v1\/responses\/([^/]+)(?:\/(cancel|input_items))?$/);
    if (m) {
      const [, rid, sub] = m;
      if (!knownIds.has(rid)) {
        jsonResponse(res, 404, {
          error: { message: `Response ${rid} not found`, type: 'invalid_request_error', code: 'not_found' },
        });
        return;
      }
      if (sub === 'cancel' && method === 'POST') {
        jsonResponse(res, 200, responseObject(rid, 'cancelled'));
        return;
      }
      if (sub === 'input_items' && method === 'GET') {
        const item = {
          id: 'msg_user_fixture_0',
          type: 'message',
          role: 'user',
          content: [{ type: 'input_text', text: 'hi' }],
        };
        jsonResponse(res, 200, { object: 'list', data: [item], first_id: item.id, last_id: item.id, has_more: false });
        return;
      }
      if (method === 'GET') {
        jsonResponse(res, 200, responseObject(rid, 'completed'));
        return;
      }
      if (method === 'DELETE') {
        knownIds.delete(rid);
        // openai-node 4.104.0 declares APIPromise<void>; a bodyless 204 is the
        // only response shape that also yields undefined at runtime.
        res.writeHead(204);
        res.end();
        return;
      }
    }

    jsonResponse(res, 404, { error: { message: 'not found', type: 'invalid_request_error', code: 'not_found' } });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;

  return {
    server,
    url: `http://127.0.0.1:${port}/v1`,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
      }),
  };
}
