import { describe, expect, it } from 'vitest';

import { parseErrorResponse } from './api';
import { explainError } from './errors';

describe('API error formats', () => {
  it.each([
    [400, 'invalid_request', false],
    [401, 'unauthorized', false],
    [403, 'forbidden', false],
    [404, 'not_found', false],
    [405, 'invalid_request', false],
    [409, 'conflict', false],
    [422, 'invalid_request', false],
    [429, 'rate_limited', true],
    [502, 'internal', true],
  ] as const)('classifies unstructured HTTP %i errors', async (status, code, retriable) => {
    const error = await parseErrorResponse(new Response('{"detail":"synthetic internal data"}', { status }));
    expect(error).toMatchObject({ status, code, retriable });
    expect(error.message).not.toContain('synthetic internal data');
    if (status === 422) {
      expect(explainError(error)).toMatchObject({ reason: '请求参数不合法', retriable: false });
    }
  });

  it('prefers the v1 error over the status fallback', async () => {
    const response = Response.json(
      { schema_version: 'v1', error: { code: 'config_invalid', message: '配置不合法', detail: 'provider' } },
      { status: 422, headers: { 'X-Trace-Id': 'tr_synthetic' } },
    );
    expect(await parseErrorResponse(response)).toMatchObject({
      code: 'config_invalid', message: '配置不合法', detail: 'provider', traceId: 'tr_synthetic',
    });
  });
});
