import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const pending = Object.keys(manifest.dependencies ?? {}).map(name => [name, root]);
const visited = new Set();
const sections = ['Third-party JavaScript runtime dependency notices\nGenerated from the installed build environment.\n'];
while (pending.length) {
  const [name, from] = pending.pop();
  const require = createRequire(path.join(from, 'package.json'));
  const filename = require.resolve(`${name}/package.json`);
  if (visited.has(filename)) continue;
  visited.add(filename);
  const dir = path.dirname(filename);
  const pkg = JSON.parse(fs.readFileSync(filename, 'utf8'));
  const files = fs.readdirSync(dir).filter(file => /^(license|licence|copying|notice|copyright)([._-]|$)/i.test(file) && fs.statSync(path.join(dir, file)).isFile());
  if (!files.length) throw new Error(`No license text for ${name} ${pkg.version}`);
  sections.push(`\n=== ${name} ${pkg.version} ===\n`);
  for (const file of files) sections.push(`\n--- ${file} ---\n${fs.readFileSync(path.join(dir, file), 'utf8')}\n`);
  for (const dep of Object.keys(pkg.dependencies ?? {})) pending.push([dep, dir]);
}
const output = path.join(root, 'public/third-party/DEPENDENCY-NOTICES.txt');
fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, sections.join(''));
console.log(`Collected notices for ${visited.size} JavaScript runtime packages.`);
