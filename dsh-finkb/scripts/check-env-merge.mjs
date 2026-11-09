/**
 * `lib/env.js` 的离线校验：不依赖 DSH、不写真实 .env。
 * 运行：node scripts/check-env-merge.mjs
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  FIELD_TO_ENV,
  SECRET_ENV,
  expandHome,
  mergeEnv,
  publicStatus,
  resolveHome,
} from '../lib/env.js';

test('mergeEnv 保留注释、空行与未知键，只改受管键', () => {
  const original = ['# FinKB', '', 'LLM_API_KEY=old-key', 'OTHER=keep-me', 'LLM_MODEL=old-model'].join('\n') + '\n';
  const next = mergeEnv(original, { LLM_API_KEY: 'new-key', LLM_MODEL: 'new-model' });
  assert.match(next, /^# FinKB/m);
  assert.match(next, /^OTHER=keep-me$/m);
  assert.match(next, /^LLM_API_KEY=new-key$/m);
  assert.match(next, /^LLM_MODEL=new-model$/m);
  assert.equal((next.match(/LLM_API_KEY=/g) ?? []).length, 1);
});

test('mergeEnv 空值/undefined 表示保持不变', () => {
  const original = 'LLM_API_KEY=keep\nSILICONFLOW_API_KEY=keep2\n';
  const next = mergeEnv(original, { LLM_API_KEY: '', SILICONFLOW_API_KEY: undefined });
  assert.equal(next, original);
});

test('mergeEnv 追加缺失键，且值含空格时加引号', () => {
  const next = mergeEnv('LLM_API_KEY=old\n', { MINERU_API_TOKEN: 'a b' });
  assert.match(next, /^MINERU_API_TOKEN="a b"$/m);
});

test('expandHome 只展开开头的 ~', () => {
  assert.equal(expandHome('~/finkb', '/home/me'), '/home/me/finkb');
  assert.equal(expandHome('/data/~x', '/home/me'), '/data/~x');
});

test('resolveHome 优先级：显式 > FINKB_HOME > ~/.finkb', () => {
  assert.equal(resolveHome({ explicit: '/a', envHome: '/b', homedir: '/home/me' }), '/a');
  assert.equal(resolveHome({ explicit: '', envHome: '/b', homedir: '/home/me' }), '/b');
  assert.equal(resolveHome({ explicit: '', envHome: '', homedir: '/home/me' }), '/home/me/.finkb');
});

test('publicStatus 不回传密钥明文', () => {
  const map = new Map([
    ['LLM_API_KEY', 'super-secret'],
    ['LLM_BASE_URL', 'https://example.com/v1'],
    ['LLM_MODEL', 'vision-model'],
  ]);
  const status = publicStatus('/home/me/finkb', '/home/me/finkb/config/.env', map, true);
  const text = JSON.stringify(status);
  assert.equal(text.includes('super-secret'), false);
  assert.equal(status.configured.llmApiKey, true);
  assert.equal(status.configured.siliconflowApiKey, false);
  assert.equal(status.values.llmBaseUrl, 'https://example.com/v1');
  assert.equal(status.values.llmModel, 'vision-model');
});

test('字段映射完整：三个密钥 + 两个可见项', () => {
  assert.deepEqual(Object.keys(FIELD_TO_ENV).sort(), [
    'llmApiKey',
    'llmBaseUrl',
    'llmModel',
    'mineruToken',
    'siliconflowApiKey',
  ]);
  assert.equal(SECRET_ENV.length, 3);
});
