/**
 * T37 criterion ③ (TypeScript): full streaming event schema through the
 * official `openai` Node SDK.  The mock server emits the native Responses SSE
 * event set; the SDK parses it into typed `ResponseStreamEvent` objects with
 * monotonic `sequence_number`.
 *
 * A mutation on the mock (dropped event / reordered) must break the sequence
 * assertions below.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import OpenAI from 'openai';
import { startMockServer, MockServer, RESPONSE_STREAM_EVENTS } from '../mock-server';

async function withClient(fn: (client: OpenAI) => Promise<void>): Promise<void> {
  const mock: MockServer = await startMockServer();
  const client = new OpenAI({ baseURL: mock.url, apiKey: 'sk-test', maxRetries: 0 });
  try {
    await fn(client);
  } finally {
    await mock.close();
  }
}

test('TS streaming parses the full official event sequence', async () => {
  await withClient(async (client) => {
    const stream = await client.responses.create({ model: 'gpt-4o', input: 'hi', stream: true });
    const types: string[] = [];
    for await (const event of stream) {
      types.push(event.type);
    }
    assert.deepEqual(types, RESPONSE_STREAM_EVENTS);
  });
});

test('TS streaming sequence_number is monotonic from 0', async () => {
  await withClient(async (client) => {
    const stream = await client.responses.create({ model: 'gpt-4o', input: 'hi', stream: true });
    const seqs: number[] = [];
    for await (const event of stream) {
      if ('sequence_number' in event) {
        seqs.push(event.sequence_number);
      }
    }
    assert.deepEqual(seqs, Array.from({ length: seqs.length }, (_, i) => i));
  });
});

test('TS streaming accumulates delta text verbatim', async () => {
  await withClient(async (client) => {
    const stream = await client.responses.create({ model: 'gpt-4o', input: 'hi', stream: true });
    const parts: string[] = [];
    for await (const event of stream) {
      if (event.type === 'response.output_text.delta') {
        parts.push(event.delta);
      }
    }
    assert.equal(parts.join(''), 'Hello, world!');
  });
});

test('TS streaming completed event carries the terminal response', async () => {
  await withClient(async (client) => {
    const stream = await client.responses.create({ model: 'gpt-4o', input: 'hi', stream: true });
    let completedStatus: string | undefined;
    let model: string | undefined;
    for await (const event of stream) {
      if (event.type === 'response.completed') {
        completedStatus = event.response.status;
        model = event.response.model;
      }
    }
    assert.equal(completedStatus, 'completed');
    assert.equal(model, 'upstream-model');
  });
});
