#!/usr/bin/env node
'use strict';
// A narrow MCP transport adapter for an operator-authorized isolated demo.
// The original server still scans scripts with STRICT=true before launch.
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');

const LAUNCH_MESSAGE = 'Launching a Godot project executes arbitrary code in its autoloads and main scene. Proceed?';
const CONFIRM_DESCRIPTION = 'Allow run_project to launch the project';
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const normalize = value => process.platform === 'win32' ? value.toLowerCase() : value;
const samePath = (left, right) => normalize(left) === normalize(right);
const plainObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value, keys) => plainObject(value) && Object.keys(value).sort().join('|') === [...keys].sort().join('|');

function authorization(gameArgument, markerArgument) {
  const game = path.resolve(gameArgument);
  const marker = path.resolve(markerArgument);
  if (path.basename(game) !== 'game' || !samePath(marker, path.join(path.dirname(game), '.pipeline_job'))) {
    throw new Error('The authorized target must be one isolated job/game with its own job marker.');
  }
  if (!samePath(fs.realpathSync(game), game) || !samePath(fs.realpathSync(marker), marker)) {
    throw new Error('Redirected authorization paths are refused.');
  }
  if (!fs.statSync(game).isDirectory() || !fs.statSync(marker).isFile() ||
      fs.readFileSync(marker, 'utf8').trim() !== 'isolated-demo-v1' ||
      !fs.statSync(path.join(game, 'project.godot')).isFile()) {
    throw new Error('An initialized isolated game project is required.');
  }
  return { game, marker };
}

function isAuthorizedProject(value, approved) {
  try {
    if (typeof value !== 'string' || !value || /[\x00-\x1f]/.test(value)) return false;
    authorization(approved.game, approved.marker); // The marker must still exist for every launch.
    return samePath(fs.realpathSync(path.resolve(value)), approved.game);
  } catch { return false; }
}

function isLaunchForm(params) {
  if (!plainObject(params) || params.message !== LAUNCH_MESSAGE ||
      (params.mode !== undefined && params.mode !== 'form')) return false;
  const schema = params.requestedSchema;
  return exactKeys(schema, ['type', 'properties', 'required']) && schema.type === 'object' &&
    exactKeys(schema.properties, ['confirm']) &&
    exactKeys(schema.properties.confirm, ['type', 'description']) &&
    schema.properties.confirm.type === 'boolean' && schema.properties.confirm.description === CONFIRM_DESCRIPTION &&
    Array.isArray(schema.required) && schema.required.length === 1 && schema.required[0] === 'confirm';
}

function mayConfirm(params, pendingTools, approved) {
  if (!isLaunchForm(params) || pendingTools.size !== 1) return false;
  const tool = pendingTools.values().next().value;
  return tool.name === 'run_project' && plainObject(tool.arguments) &&
    isAuthorizedProject(tool.arguments.projectPath, approved);
}

function safeStderr(value) {
  return value.replace(/(\bBearer\s+)[A-Za-z0-9._~+/=\-]+/gi, '$1[REDACTED]')
    .replace(/(\b(?:sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&\s"<>]+/gi, '$1[REDACTED]')
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[REDACTED]');
}

function main() {
  if (process.argv.length !== 5) throw new Error('Usage: node wrapper.cjs <runtime-index.js> <authorized-game> <job-marker>');
  const [runtimeArgument, gameArgument, markerArgument] = process.argv.slice(2);
  const runtime = path.resolve(runtimeArgument);
  if (!fs.statSync(runtime).isFile()) throw new Error('The existing MCP runtime entry is required.');
  const approved = authorization(gameArgument, markerArgument);
  const childEnv = { ...process.env, GODOT_MCP_STRICT: 'true' };
  for (const key of Object.keys(childEnv)) {
    const upper = key.toUpperCase();
    if (upper.endsWith('_API_KEY') || upper.endsWith('_AUTH_TOKEN') ||
        ['CODEX_ACCESS_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'GAME_CLAUDE_KEY', 'GAME_RESEARCH_KEY',
         'GODOT_MCP_DISABLE_SECURITY', 'GODOT_MCP_DISABLE_ELICITATION'].includes(upper)) delete childEnv[key];
  }
  const backend = spawn(process.execPath, [runtime], { env: childEnv, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  backend.stdin.on('error', () => {}); // A closing server must not emit an unhandled EPIPE.
  const pendingTools = new Map();
  const toBackend = message => backend.stdin.write(JSON.stringify(message) + '\n');
  const toClient = message => process.stdout.write(JSON.stringify(message) + '\n');
  const rejectTool = (id, text) => toClient({ jsonrpc: '2.0', id,
    result: { isError: true, content: [{ type: 'text', text }] } });
  const clientLines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  clientLines.on('line', line => {
    let message;
    try { message = JSON.parse(line); } catch { process.stderr.write('[PREAUTHORIZED] Invalid client JSON refused.\n'); return; }
    if (message.method === 'initialize' && plainObject(message.params)) {
      const caps = message.params.capabilities ?? {};
      message.params.capabilities = { ...caps, elicitation: { ...(caps.elicitation ?? {}), form: {} } };
    }
    if (message.method === 'tools/call' && own(message, 'id')) {
      if (pendingTools.has(message.id)) { rejectTool(message.id, 'Duplicate pending tool request refused.'); return; }
      if (message.params?.name === 'run_project' && !isAuthorizedProject(message.params.arguments?.projectPath, approved)) {
        rejectTool(message.id, 'This project launch is outside the existing operator authorization.'); return;
      }
      pendingTools.set(message.id, message.params ?? {});
    }
    toBackend(message);
  });
  const serverLines = readline.createInterface({ input: backend.stdout, crlfDelay: Infinity });
  serverLines.on('line', line => {
    let message;
    try { message = JSON.parse(line); } catch { process.stderr.write('[PREAUTHORIZED] Invalid backend JSON suppressed.\n'); return; }
    if (message.method === 'elicitation/create' && own(message, 'id')) {
      const accept = mayConfirm(message.params, pendingTools, approved);
      toBackend({ jsonrpc: '2.0', id: message.id, result: accept ?
        { action: 'accept', content: { confirm: true } } : { action: 'decline' } });
      process.stderr.write(accept ?
        `[PREAUTHORIZED] Accepted run_project for ${approved.game} using existing operator task authorization; no live human click was recorded. STRICT scanning remains enabled.\n` :
        '[PREAUTHORIZED] Declined elicitation outside the single authorized project launch.\n');
      return;
    }
    if (!own(message, 'method') && own(message, 'id')) pendingTools.delete(message.id);
    toClient(message);
  });
  readline.createInterface({ input: backend.stderr, crlfDelay: Infinity })
    .on('line', line => process.stderr.write(safeStderr(line) + '\n'));
  clientLines.on('close', () => {
    if (backend.stdin.writable) backend.stdin.end();
    const timer = setTimeout(() => { if (backend.exitCode === null) backend.kill(); }, 2000);
    timer.unref();
  });
  backend.on('error', () => { process.stderr.write('[PREAUTHORIZED] MCP runtime failed to start.\n'); process.exitCode = 1; });
  backend.on('close', code => { clientLines.close(); process.exitCode = code ?? 1; process.stdin.pause(); });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => backend.kill());
}

module.exports = { authorization, isAuthorizedProject, isLaunchForm, mayConfirm };
if (require.main === module) {
  try { main(); } catch { process.stderr.write('[PREAUTHORIZED] Invalid isolated-project authorization; runtime was not started.\n'); process.exitCode = 2; }
}
