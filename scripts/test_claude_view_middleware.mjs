import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { after, beforeEach, test } from 'node:test';

// Replace the built-in before loading the middleware. No Python, account, CLI,
// network or real session operation can run from this suite.
const originalExecFile = childProcess.execFile;
let calls = [];
let implementation;
childProcess.execFile = (file, args, options, callback) => {
  calls.push({ file, args, options });
  return implementation(file, args, options, callback);
};
syncBuiltinESMExports();
const { allowedLocalRead, claudeLocalView } = await import('./claude-view-middleware.mjs');
after(() => {
  childProcess.execFile = originalExecFile;
  syncBuiltinESMExports();
});
beforeEach(() => {
  calls = [];
  implementation = (_file, _args, _options, callback) => {
    queueMicrotask(() => callback(null, JSON.stringify({ profiles: [], source: 'test-fixture' }), 'IGNORED_STDERR_SECRET'));
    return {};
  };
});

const id = '11111111-1111-4111-8111-111111111111';
function request(url = '/__claude_local/overview', changes = {}) {
  return { method: 'GET', url, headers: { host: '127.0.0.1:1547', 'sec-fetch-site': 'same-origin',
    origin: 'http://127.0.0.1:1547' }, socket: { remoteAddress: '127.0.0.1' }, ...changes };
}
function response() {
  return { statusCode: 200, headers: {}, body: null,
    setHeader(key, value) { this.headers[key.toLowerCase()] = value; },
    end(value) { this.body = value; } };
}
function handler() {
  let middleware;
  claudeLocalView().configureServer({ middlewares: { use(value) { middleware = value; } } });
  return middleware;
}
async function invoke(req) {
  const res = response();
  let next = 0;
  await handler()(req, res, () => { next += 1; });
  return { res, next };
}

test('only loopback GET with same origin is admitted', () => {
  assert.equal(allowedLocalRead(request()), true);
  assert.equal(allowedLocalRead(request(undefined, { socket: { remoteAddress: '::ffff:127.0.0.1' } })), true);
  assert.equal(allowedLocalRead(request(undefined, { headers: { host: '127.0.0.1:1547' } })), true);
  for (const method of ['POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD']) {
    assert.equal(allowedLocalRead(request(undefined, { method })), false, method);
  }
  for (const host of ['attacker.invalid:1547', 'localhost:1547', '127.0.0.1.attacker.invalid:1547',
    '127.0.0.1', '[::1]:1547', '127.0.0.1:1547@attacker.invalid', ['127.0.0.1:1547']]) {
    assert.equal(allowedLocalRead(request(undefined, { headers: { host } })), false, String(host));
  }
  for (const remoteAddress of ['192.0.2.1', '::1', '::ffff:192.0.2.1', '127.0.0.2']) {
    assert.equal(allowedLocalRead(request(undefined, { socket: { remoteAddress } })), false, remoteAddress);
  }
});

test('cross-site and wrong-port origins never start the helper', async () => {
  for (const origin of ['http://attacker.invalid', 'null', 'http://127.0.0.1:9999', 'https://127.0.0.1:1547']) {
    const { res } = await invoke(request(undefined, { headers: { host: '127.0.0.1:1547', origin } }));
    assert.equal(res.statusCode, 403);
  }
  for (const site of ['cross-site', 'same-site']) {
    const { res } = await invoke(request(undefined, { headers: { host: '127.0.0.1:1547', 'sec-fetch-site': site } }));
    assert.equal(res.statusCode, 403);
  }
  assert.equal(calls.length, 0);
});

test('valid overview uses fixed isolated Python arguments and a safe environment', async () => {
  const { res } = await invoke(request());
  assert.equal(res.statusCode, 200);
  assert.equal(calls.length, 1);
  const call = calls[0];
  assert.deepEqual(call.args.slice(0, 2), ['-I', '-B']);
  assert.equal(call.args[3], 'overview');
  assert.equal(call.args[4], '--registry');
  assert.ok(call.args[5].endsWith('/.cockpit_accounts/claude_native/registry.json'));
  assert.equal(call.options.timeout, 55000);
  assert.equal(call.options.maxBuffer, 256 * 1024);
  assert.deepEqual(Object.keys(call.options.env).sort(), ['HOME', 'LANG', 'PATH']);
  assert.equal(res.headers['cache-control'], 'no-store');
  assert.equal(res.headers['x-content-type-options'], 'nosniff');
  assert.equal(res.headers['cross-origin-resource-policy'], 'same-origin');
  assert.ok(res.headers['content-type'].startsWith('application/json'));
  assert.ok(!res.body.includes('IGNORED_STDERR_SECRET'));
});

test('only fixed read routes exist and mutation routes are rejected', async () => {
  for (const route of ['resume', 'resume-child', 'snapshot', 'login_cli', 'handoff', 'logout', 'credentials', 'overview/extra']) {
    const { res } = await invoke(request(`/__claude_local/${route}`));
    assert.equal(res.statusCode, 404, route);
  }
  const { next } = await invoke(request('/ordinary-app-page'));
  assert.equal(next, 1);
  assert.equal(calls.length, 0);
});

test('sessions require a canonical UUID and never receive arbitrary filesystem arguments', async () => {
  for (const value of ['', '../../private', '--registry=/etc/passwd', 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA', `${id}/../other`]) {
    const { res } = await invoke(request(`/__claude_local/sessions?profileId=${encodeURIComponent(value)}`));
    assert.equal(res.statusCode, 404, value);
  }
  assert.equal(calls.length, 0);
  const { res } = await invoke(request(`/__claude_local/sessions?profileId=${id}&registry=/etc/passwd&path=/private`));
  assert.equal(res.statusCode, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].args[3], 'sessions');
  assert.deepEqual(calls[0].args.slice(-2), ['--profile-id', id]);
  assert.ok(!calls[0].args.includes('/etc/passwd'));
  assert.ok(!calls[0].args.includes('/private'));
});

test('invalid request URL yields a bounded safe response rather than a rejected promise', async () => {
  const { res } = await invoke(request('http://['));
  assert.equal(res.statusCode, 400);
  assert.deepEqual(JSON.parse(res.body), { error: 'Invalid request' });
  assert.equal(calls.length, 0);
});

test('helper failure and malformed JSON never expose raw stdout/stderr/exception', async () => {
  implementation = (_file, _args, _options, callback) => {
    queueMicrotask(() => callback(new Error('SECRET_EXCEPTION'), 'SECRET_STDOUT', 'SECRET_STDERR'));
    return {};
  };
  const failed = await invoke(request());
  assert.equal(failed.res.statusCode, 503);
  assert.ok(!failed.res.body.includes('SECRET_'));
  implementation = (_file, _args, _options, callback) => {
    queueMicrotask(() => callback(null, 'SECRET_INVALID_JSON', 'SECRET_STDERR'));
    return {};
  };
  const malformed = await invoke(request());
  assert.equal(malformed.res.statusCode, 503);
  assert.ok(!malformed.res.body.includes('SECRET_'));
});

test('identical concurrent reads share one helper and completed requests are not cached', async () => {
  let finish;
  implementation = (_file, _args, _options, callback) => { finish = callback; return {}; };
  const first = invoke(request(`/__claude_local/sessions?profileId=${id}`));
  const second = invoke(request(`/__claude_local/sessions?profileId=${id}`));
  assert.equal(calls.length, 1);
  finish(null, JSON.stringify({ sessions: [], checkedAt: 1700000000 }));
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.res.body, b.res.body);
  implementation = (_file, _args, _options, callback) => { callback(null, '{}'); return {}; };
  await invoke(request(`/__claude_local/sessions?profileId=${id}`));
  assert.equal(calls.length, 2);
});
