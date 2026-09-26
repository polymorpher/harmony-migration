#!/usr/bin/env node
/*
 * Run the JavaScript engine (the one embedded in the offline HTML verifier)
 * from the command line. Used by the cross-engine conformance test; the Python
 * CLI (toolkit/scripts/random-sample-verify.py) remains the authoritative tool.
 *
 *   node toolkit/verifier/node-run.js --bundle DIR --sample-size 25 --seed 20260925
 *
 * Prints the JSON report on stdout and exits with the report's exit code.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const core = require('./sample_core.js');

const ROOT = path.resolve(__dirname, '..', '..');

function parseArgs(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const next = () => { i += 1; if (i >= argv.length) throw new Error(`${flag} needs a value`); return argv[i]; };
    if (flag === '--bundle') options.bundle = next();
    else if (flag === '--sample-size') options.sampleSize = Number(next());
    else if (flag === '--seed') options.seed = next();
    else if (flag === '--sample-by') options.sampleBy = next();
    else if (flag === '--show-row') options.showRow = next();
    else if (flag === '--expected-manifest-sha256') options.expectedManifestSha256 = next();
    else if (flag === '--verify-all-metadata') options.verifyAllMetadata = true;
    else if (flag === '--require-chain-truth') options.requireChainTruth = true;
    else throw new Error(`unknown argument ${flag}`);
  }
  if (!options.bundle) throw new Error('--bundle is required');
  if (options.sampleSize !== undefined && !(Number.isInteger(options.sampleSize) && options.sampleSize >= 0)) {
    throw new Error('--sample-size must be a non-negative integer');
  }
  return options;
}

function directoryAdapter(root) {
  function walk(dir, prefix, out) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(path.join(dir, entry.name), relative, out);
      else if (entry.isFile()) out.push(relative);
    }
    return out;
  }
  return {
    name: root,
    async list() { return fs.existsSync(root) && fs.statSync(root).isDirectory() ? walk(root, '', []).sort() : []; },
    async size(relative) { return fs.statSync(path.join(root, relative)).size; },
    async readBytes(relative) { return new Uint8Array(fs.readFileSync(path.join(root, relative))); },
    async *readChunks(relative) {
      for await (const chunk of fs.createReadStream(path.join(root, relative), { highWaterMark: core.CHUNK })) {
        yield new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength);
      }
    },
  };
}

async function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    return core.EXIT.INPUT;
  }
  const rulesBytes = fs.readFileSync(path.join(__dirname, 'rules.json'));
  const rules = core.parseJsonStrict(rulesBytes.toString('utf-8'), 'rules');
  const snapshot = core.parseJsonStrict(fs.readFileSync(path.join(ROOT, rules.pinned_snapshot), 'utf-8'), 'snapshot');
  try {
    const report = await core.verifyBundle(directoryAdapter(options.bundle), {
      ...options,
      rules,
      snapshot,
      rulesSha256: new core.Sha256().update(new Uint8Array(rulesBytes)).hexdigest(),
    });
    process.stdout.write(JSON.stringify(report, null, 2) + '\n');
    return report.exit_code;
  } catch (error) {
    if (error instanceof core.BundleError) {
      process.stderr.write(`error: ${error.message}\n`);
      return core.EXIT.INPUT;
    }
    throw error;
  }
}

main().then((code) => { process.exitCode = code; });
