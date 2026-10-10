#!/usr/bin/env node
'use strict';

// Run in SnowLuma's container/network namespace with the host and port from
// highway_put_start. This tests ONLY direct TCP reachability, not QQ auth/upload.
const net = require('node:net');
const [host, portText, timeoutText = '10000'] = process.argv.slice(2);
const port = Number(portText);
const timeout = Number(timeoutText);
if (!host || !Number.isInteger(port) || port < 1 || port > 65535 ||
    !Number.isInteger(timeout) || timeout < 1) {
  console.error('Usage: node check_highway.cjs <Highway host> <port> [timeout-ms=10000]');
  process.exit(2);
}

const start = Date.now();
const socket = net.createConnection({ host, port });
let finished = false;
function finish(status, error) {
  if (finished) return;
  finished = true;
  socket.destroy();
  console.log(JSON.stringify({ host, port, status, elapsed_ms: Date.now() - start,
    ...(error ? { error } : {}) }));
  process.exitCode = status === 'connected' ? 0 : 1;
}
// A wall-clock timer also covers DNS resolution, unlike socket idle timeouts.
const timer = setTimeout(() => finish('failed', 'TCP_CONNECT_TIMEOUT'), timeout);
socket.once('connect', () => { clearTimeout(timer); finish('connected'); });
socket.once('error', (error) => { clearTimeout(timer); finish('failed', error.code || 'TCP_ERROR'); });
