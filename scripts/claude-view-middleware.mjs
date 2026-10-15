import { execFile } from 'node:child_process';
import { userInfo } from 'node:os';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';

const helper = fileURLToPath(new URL('./claude-local-companion.py', import.meta.url));
const home = userInfo().homedir;
const registry = `${home}/.cockpit_accounts/claude_native/registry.json`;
const python = ['/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3'].find(existsSync);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const pending = new Map();

export function allowedLocalRead(req) {
  if (req.method !== 'GET') return false;
  const host = req.headers.host;
  if (typeof host !== 'string' || !/^127\.0\.0\.1:\d+$/.test(host)) return false;
  if (req.socket.remoteAddress !== '127.0.0.1' && req.socket.remoteAddress !== '::ffff:127.0.0.1') return false;
  const site = req.headers['sec-fetch-site'];
  if (site && site !== 'same-origin' && site !== 'none') return false;
  const origin = req.headers.origin;
  return !origin || origin === `http://${host}`;
}

function readView(action, profileId) {
  const key = `${action}:${profileId || ''}`;
  if (pending.has(key)) return pending.get(key);
  const args = ['-I', '-B', helper, action, '--registry', registry];
  if (profileId) args.push('--profile-id', profileId);
  const promise = new Promise((resolve, reject) => {
    execFile(python, args, {
      env: { HOME: home, PATH: '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin', LANG: 'en_US.UTF-8' },
      timeout: 55000, maxBuffer: 256 * 1024,
    }, (error, stdout) => {
      if (error) return reject(new Error('Local account view unavailable'));
      try { resolve(JSON.parse(stdout)); } catch { reject(new Error('Invalid local view')); }
    });
  }).finally(() => pending.delete(key));
  pending.set(key, promise);
  return promise;
}

export function claudeLocalView() {
  return {
    name: 'claude-local-readonly-view',
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        let url;
        try { url = new URL(req.url || '/', 'http://127.0.0.1'); }
        catch {
          res.statusCode = 400;
          res.setHeader('Content-Type', 'application/json');
          return res.end('{"error":"Invalid request"}');
        }
        if (!url.pathname.startsWith('/__claude_local/')) return next();
        res.setHeader('Cache-Control', 'no-store');
        res.setHeader('Content-Type', 'application/json; charset=utf-8');
        res.setHeader('X-Content-Type-Options', 'nosniff');
        res.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
        if (!allowedLocalRead(req)) { res.statusCode = 403; return res.end('{"error":"Local read only"}'); }
        const action = url.pathname === '/__claude_local/overview' ? 'overview'
          : url.pathname === '/__claude_local/sessions' ? 'sessions' : null;
        const profileId = url.searchParams.get('profileId');
        if (!action || (action === 'sessions' && (!profileId || !uuid.test(profileId)))) {
          res.statusCode = 404; return res.end('{"error":"Unknown view"}');
        }
        try { res.end(JSON.stringify(await readView(action, profileId))); }
        catch { res.statusCode = 503; res.end('{"error":"Local view unavailable"}'); }
      });
    },
  };
}
