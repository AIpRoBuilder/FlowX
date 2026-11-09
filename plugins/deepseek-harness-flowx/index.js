// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-09: register the owner-only DeepSeek Harness host channel.
// Modified 2026-11-09: use the authenticated shared Fetch route with bounded JSON.

import { FlowXHostService } from './host/service.js';

export const name = 'deepseek-harness-flowx';
export const inject = ['connection'];

const LIMIT = 1024 * 1024;
const OWNER = { principal: { kind: 'owner' } };
const failure = (code, message) => ({ ok: false, error: { code: `flowx/${code}`, message, details: {} } });
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});

export function apply(ctx) {
  const service = new FlowXHostService();
  // Harness authenticates the shared /api carrier and its exact-route registry
  // admits only owner principals. Do not mount this handler outside that boundary.
  ctx.connection.fetch.register({
    path: '/api/flowx', methods: ['POST'], requestBody: 'buffered',
    async fetch(request) {
      try {
        if (request.headers.get('content-type')?.split(';', 1)[0].trim().toLowerCase() !== 'application/json') {
          return json(failure('unsupported-media-type', 'Content type must be application/json.'), 415);
        }
        // Harness buffers first under its shared cap (300 MiB by default); this
        // route's smaller cap is enforced on the actual bytes, not the header.
        const bytes = await request.arrayBuffer();
        if (bytes.byteLength > LIMIT) return json(failure('request-too-large', 'Request body exceeds 1 MiB.'), 413);
        let body;
        try { body = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
        catch { return json(failure('invalid-request', 'Invalid JSON request.'), 400); }
        if (!body || typeof body !== 'object' || Array.isArray(body) ||
            Object.keys(body).length !== 2 || !Object.hasOwn(body, 'endpoint') ||
            !Object.hasOwn(body, 'payload') || typeof body.endpoint !== 'string') {
          return json(failure('invalid-request', 'Expected endpoint and payload.'), 400);
        }
        return json(await service.dispatch(body.endpoint, body.payload, OWNER));
      } catch {
        return json(failure('internal', 'The operation failed.'), 500);
      }
    },
  });
  // Connection owns its registration; this fiber separately owns subprocesses.
  ctx.effect(() => () => service.dispose(), 'flowx: host cleanup');
}