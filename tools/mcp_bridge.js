#!/usr/bin/env node
/**
 * ChatGPT MCP Bridge
 * 将 ChatGPT 的 STDIO 协议桥接到 LocalAgent 的 Streamable HTTP MCP 端点。
 *
 * 背景：ChatGPT 桌面客户端的"流式 HTTP"类型强制要求 OAuth 流程，
 * 本地 MCP 服务无法满足。本脚本通过 mcp-remote 将 STDIO 翻译为
 * Streamable HTTP 请求，让 ChatGPT 接入本地 MCP。
 *
 * ChatGPT 配置：
 *   类型：STDIO
 *   启动命令：node
 *   参数：tools/mcp_bridge.js
 *   工作目录：项目根目录（f:\<project_root>）
 *
 * 环境变量：
 *   LOCALAGENT_MCP_URL  自定义 MCP 端点（默认 http://127.0.0.1:8766/mcp）
 *
 * 原有的 Trae/CatPaw 流式 HTTP 直连配置不受影响。
 */
const { spawn, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const MCP_HTTP_URL = process.env.LOCALAGENT_MCP_URL || 'http://127.0.0.1:8766/mcp';

// 候选 mcp-remote 安装路径（按优先级尝试）
function resolveProxyJs() {
  const candidates = [];

  // 1. 当前 PATH 中的 npm 全局根
  try {
    const globalRoot = execSync('npm root -g', { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }).trim();
    candidates.push(path.join(globalRoot, 'mcp-remote', 'dist', 'proxy.js'));
  } catch (e) { /* ignore */ }

  // 2. 常见 Windows 全局路径
  const appdata = process.env.APPDATA || '';
  const localappdata = process.env.LOCALAPPDATA || '';
  const userhome = process.env.USERPROFILE || process.env.HOME || '';
  if (appdata) {
    candidates.push(path.join(appdata, 'npm', 'node_modules', 'mcp-remote', 'dist', 'proxy.js'));
  }
  // 3. TRAE 自带 Node 的全局路径（TRAE IDE 内 PATH 优先命中此 npm）
  if (appdata) {
    candidates.push(path.join(appdata, 'TRAE SOLO CN', 'ModularData', 'ai-agent', 'vm', 'tools', 'node', 'node_modules', 'mcp-remote', 'dist', 'proxy.js'));
  }
  // 4. nvm-windows / volta / scoop 等常见位置
  if (localappdata) {
    candidates.push(path.join(localappdata, 'npm', 'node_modules', 'mcp-remote', 'dist', 'proxy.js'));
  }
  if (userhome) {
    candidates.push(path.join(userhome, 'AppData', 'Roaming', 'npm', 'node_modules', 'mcp-remote', 'dist', 'proxy.js'));
    candidates.push(path.join(userhome, '.npm-global', 'lib', 'node_modules', 'mcp-remote', 'dist', 'proxy.js'));
    candidates.push(path.join(userhome, '.volta', 'tools', 'image', 'packages', 'mcp-remote', 'lib', 'node_modules', 'mcp-remote', 'dist', 'proxy.js'));
  }

  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

const PROXY_JS = resolveProxyJs();

function startChild(cmd, args) {
  return spawn(cmd, args, {
    stdio: 'inherit',
    windowsHide: true,
    shell: process.platform === 'win32',  // Windows 上需要 shell 才能找到 .cmd/.bat
  });
}

let child;
if (PROXY_JS) {
  // 找到了 proxy.js，直接用 node 跑
  child = spawn(process.execPath, [PROXY_JS, MCP_HTTP_URL], {
    stdio: 'inherit',
    windowsHide: true,
  });
} else {
  // 兜底：用 npx 启动（会自动查找/下载 mcp-remote）
  process.stderr.write(`[mcp_bridge] 未在常见路径找到 mcp-remote，使用 npx 启动（首次可能有延迟）\n`);
  const npxCmd = process.platform === 'win32' ? 'npx.cmd' : 'npx';
  child = startChild(npxCmd, ['-y', 'mcp-remote', MCP_HTTP_URL]);
}

child.on('error', (err) => {
  process.stderr.write(`[mcp_bridge] 启动失败: ${err.message}\n`);
  process.stderr.write(`[mcp_bridge] 请先运行: npm install -g mcp-remote\n`);
  process.exit(1);
});

child.on('exit', (code, signal) => {
  process.exit(code ?? (signal ? 130 : 0));
});

// 转发终止信号给子进程
process.on('SIGINT', () => child.kill('SIGINT'));
process.on('SIGTERM', () => child.kill('SIGTERM'));
