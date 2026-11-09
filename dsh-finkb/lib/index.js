/**
 * FinKB 设置页 —— Host 半。
 *
 * 只做两件事：
 * 1. 在 DSH 的 Web 服务上挂一个同源接口 `/finkb-settings/api`（读状态 / 写 .env）；
 * 2. 把界面提交的密钥写进 `$FINKB_HOME/config/.env`。
 *
 * 为什么写完不用重启：FinKB 每次调用 LLM/解析时都会重新读 `.env`
 * （`LLMConfig.from_env()` 内部 `load_env_file()`），所以保存后下一次工具调用就生效。
 */
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import z from '@deepseek-ai/schemastery';
import {
  FIELD_TO_ENV,
  mergeEnv,
  publicStatus,
  readEnv,
  resolveHome,
  writeEnv,
} from './env.js';

export const name = 'finkb-settings';

/** webServer 是可选服务：纯对话部署没有它时，插件仍可加载，只是没有设置接口。 */
export const inject = [];

export const Config = z.object({
  // 知识库目录是部署期配置（FINKB_HOME），不放在运行期设置里：
  // 这个卡片只负责写 .env，改 Key 能热加载，改目录不能（MCP 子进程启动时已固定）。
});

const MAX_BODY_BYTES = 64 * 1024;

/** 知识库目录只由部署环境决定：FINKB_HOME > ~/.finkb。 */
function currentHome() {
  return path.resolve(
    resolveHome({ envHome: process.env.FINKB_HOME || '', homedir: os.homedir() }),
  );
}

async function envPathFor(home) {
  return path.join(home, 'config', '.env');
}

async function readEnvText(envPath) {
  try {
    return await fs.readFile(envPath, 'utf8');
  } catch (error) {
    if (error && error.code === 'ENOENT') return '';
    throw error;
  }
}

async function statusFor() {
  const home = currentHome();
  const envPath = await envPathFor(home);
  const map = await readEnv(envPath);
  const exists = await fs
    .access(envPath)
    .then(() => true)
    .catch(() => false);
  return publicStatus(home, envPath, map, exists);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('请求体过大'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try {
        const raw = Buffer.concat(chunks).toString('utf8');
        resolve(raw === '' ? {} : JSON.parse(raw));
      } catch {
        reject(new Error('请求体不是合法 JSON'));
      }
    });
    req.on('error', reject);
  });
}

function writeJson(res, statusCode, value) {
  const body = JSON.stringify(value);
  res.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  });
  res.end(body);
}

function parseAuthority(value) {
  try {
    return new URL('http://' + value);
  } catch {
    return undefined;
  }
}

function isLoopbackHostname(hostname) {
  const name = String(hostname).toLowerCase().replace(/^\[|\]$/g, '');
  return name === 'localhost' || name === '127.0.0.1' || name === '::1';
}

/** 同源 + 本地/受信 Host：默认只允许本机页面调用这个接口。 */
function trustedRequest(req, trustedHosts = []) {
  const host = req.headers?.host;
  if (!host) return false;
  const hostUrl = parseAuthority(host);
  if (!hostUrl) return false;
  const allowed = isLoopbackHostname(hostUrl.hostname)
    ? true
    : trustedHosts.some((entry) => {
        const value = typeof entry === 'string' ? entry : entry?.host;
        return typeof value === 'string' && value === hostUrl.host;
      });
  if (!allowed) return false;
  if (req.headers['sec-fetch-site'] === 'cross-site') return false;
  const origin = req.headers.origin;
  if (!origin) return true;
  try {
    return new URL(origin).host === hostUrl.host;
  } catch {
    return false;
  }
}

async function handleApi(req, res, logger, trustedHosts) {
  if (!trustedRequest(req, trustedHosts)) {
    writeJson(res, 403, { ok: false, message: 'forbidden' });
    return;
  }
  if (req.method !== 'POST') {
    res.writeHead(405, { allow: 'POST' });
    res.end();
    return;
  }

  try {
    const body = await readBody(req);
    const action = body?.action;

    if (action === 'status') {
      writeJson(res, 200, { ok: true, ...(await statusFor()) });
      return;
    }

    if (action === 'save') {
      const home = currentHome();
      const envPath = await envPathFor(home);
      const previous = await readEnvText(envPath);
      const updates = {};
      for (const [field, envKey] of Object.entries(FIELD_TO_ENV)) {
        const value = body?.values?.[field];
        if (typeof value === 'string') updates[envKey] = value;
      }
      const next = mergeEnv(previous, updates);
      await writeEnv(envPath, next);
      const map = await readEnv(envPath);
      logger?.info?.(`settings saved to ${envPath}`);
      writeJson(res, 200, { ok: true, ...publicStatus(home, envPath, map, true) });
      return;
    }

    writeJson(res, 400, { ok: false, message: `未知 action：${String(action)}` });
  } catch (error) {
    logger?.warn?.(`settings api error: ${error?.message ?? error}`);
    writeJson(res, 400, { ok: false, message: error instanceof Error ? error.message : String(error) });
  }
}

function mountApi(wctx, logger, trustedHosts) {
  return wctx.webServer.register({
    kind: 'prefix',
    path: '/finkb-settings/api',
    handler: (req, res) => handleApi(req, res, logger, trustedHosts),
  });
}

export function apply(ctx) {
  const logger = ctx.logger('finkb-settings');
  // webStartup 是 DSH 0.2.x 新服务，webRuntime 是旧名；两者取其一，避免重复挂载。
  const candidates = new Map();
  let active;
  let dispose;

  const reconcile = () => {
    const next = candidates.get('webStartup') ?? candidates.get('webRuntime');
    if (next === active) return;
    dispose?.();
    dispose = undefined;
    active = next;
    if (next) {
      dispose = mountApi(next.ctx, logger, next.trustedHosts);
      logger.info?.('FinKB 设置接口已挂载：/finkb-settings/api');
    }
  };

  for (const service of ['webStartup', 'webRuntime']) {
    ctx.inject(['webServer', service], (wctx) => {
      wctx.effect(() => {
        const candidate = { ctx: wctx, trustedHosts: wctx[service]?.trustedHosts ?? [] };
        candidates.set(service, candidate);
        reconcile();
        return () => {
          if (candidates.get(service) === candidate) candidates.delete(service);
          reconcile();
        };
      });
    });
  }
}
