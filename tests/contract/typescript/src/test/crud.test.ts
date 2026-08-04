/**
 * T37 criterion ① (TypeScript): six official Responses operations through the
 * official `openai` Node SDK -- create / retrieve / delete / cancel / compact /
 * input_items.  Zero vendor-specific code: every call is a standard
 * `client.responses.*` call; the only scaffolding is the localhost mock server
 * (`src/mock-server.ts`), which is a plain HTTP stand-in, not SDK-specific.
 *
 * openai-node version note (4.104.0):
 * - ``delete`` is exposed as ``del`` and is typed ``void``; an empty 204 body
 *   is normalised to ``null`` by the 4.104.0 runtime JSON decoder.
 * - ``cancel`` is typed ``void``.
 * - ``compact`` is NOT part of the ``Responses`` resource in 4.104.0 -- the
 *   official Node SDK simply does not expose it.  Compact is therefore covered
 *   on the Python side (openai 2.53.0 has ``compact``) and marked "SDK layer
 *   not reachable on TypeScript 4.104.0" here.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import OpenAI from 'openai';
import { startMockServer, MockServer } from '../mock-server';

async function withClient(fn: (client: OpenAI) => Promise<void>): Promise<void> {
  const mock: MockServer = await startMockServer();
  const client = new OpenAI({ baseURL: mock.url, apiKey: 'sk-test', maxRetries: 0 });
  try {
    await fn(client);
  } finally {
    await mock.close();
  }
}

test('TS create returns an official response object', async () => {
  await withClient(async (client) => {
    const r = await client.responses.create({ model: 'gpt-4o', input: 'hi' });
    assert.equal(r.object, 'response');
    assert.match(r.id, /^resp_/);
    assert.equal(r.status, 'in_progress');
    assert.equal(r.model, 'gpt-4o');
    assert.ok(Array.isArray(r.output));
  });
});

test('TS retrieve returns the response object', async () => {
  await withClient(async (client) => {
    const created = await client.responses.create({ model: 'gpt-4o', input: 'hi' });
    const got = await client.responses.retrieve(created.id);
    assert.equal(got.id, created.id);
    assert.equal(got.object, 'response');
  });
});

test('TS retrieve on unknown id raises a typed API error', async () => {
  await withClient(async (client) => {
    await assert.rejects(
      async () => {
        await client.responses.retrieve('resp_does_not_exist_0000');
      },
      (err: unknown) => {
        assert.ok(err instanceof OpenAI.APIError);
        assert.equal((err as { status?: number }).status, 404);
        return true;
      },
    );
  });
});

test('TS del returns the 4.104.0 empty-body value and removes the response', async () => {
  await withClient(async (client) => {
    const created = await client.responses.create({ model: 'gpt-4o', input: 'hi' });
    const result = await client.responses.del(created.id);
    // The declaration is APIPromise<void>; at runtime the SDK JSON decoder
    // normalises an empty 204 body to null.  Pin the observed 4.104.0 contract.
    assert.equal(result, null);
    await assert.rejects(
      async () => client.responses.retrieve(created.id),
      (err: unknown) => err instanceof OpenAI.APIError && (err as { status?: number }).status === 404,
    );
  });
});

test('TS cancel completes without throwing (official SDK returns void)', async () => {
  await withClient(async (client) => {
    const created = await client.responses.create({ model: 'gpt-4o', input: 'hi' });
    await client.responses.cancel(created.id);
    // cancel() is void in openai-node 4.104.0; no-throw is the contract.
    assert.ok(true);
  });
});

test('TS compact is not exposed by openai-node 4.104.0 (marked Python-side)', async () => {
  // openai-node 4.104.0 does not expose `responses.compact`.  We assert the
  // absence explicitly so a future SDK bump surfaces this test as the moment
  // the Node-side compact contract can be sealed.
  await withClient(async (client) => {
    assert.equal(typeof (client.responses as unknown as Record<string, unknown>).compact, 'undefined');
  });
});

test('TS input_items lists persisted input as official list', async () => {
  await withClient(async (client) => {
    const created = await client.responses.create({ model: 'gpt-4o', input: 'hi' });
    const lst = await client.responses.inputItems.list(created.id);
    // CursorPage<ResponseItem> has no `object` field in 4.104.0; the raw
    // response shape is verified via the data / has_more surface.
    assert.ok(Array.isArray(lst.data));
    assert.equal(lst.data.length, 1);
    assert.equal(lst.data[0].type, 'message');
    assert.equal(lst.has_more, false);
  });
});
