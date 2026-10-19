// Real local HTTP round trip: JSON bodies must survive long-operation transport.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../electron/main.js'), 'utf8');
const snippet = source.slice(source.indexOf('async function fetchJson('), source.indexOf('async function serviceHealthyOnPort('));
const fetchJson = vm.runInNewContext(`${snippet}; fetchJson`, { http, Buffer, Error, Promise });
const server = http.createServer((request, response) => {
  let body = '';
  request.on('data', chunk => { body += chunk; });
  request.on('end', () => {
    if (request.url === '/error') {
      response.writeHead(409, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ detail: 'operation already running' }));
      return;
    }
    setTimeout(() => {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ body: JSON.parse(body), length: Number(request.headers['content-length']) }));
    }, 100);
  });
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  try {
    const body = JSON.stringify({ excluded_packages: ['cask:ngrok'], text: 'עברית' });
    const result = await fetchJson(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    assert.deepEqual(JSON.parse(JSON.stringify(result.body)), JSON.parse(body));
    assert.equal(result.length, Buffer.byteLength(body));
    await assert.rejects(fetchJson(`${url}/error`), /operation already running/);
    process.stdout.write('HTTP JSON body, delayed response, and error propagation: PASS\n');
  } finally { server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
