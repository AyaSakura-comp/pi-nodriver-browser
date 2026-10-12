// Runs the real worker launcher from inside a disposable "parent" systemd unit (a stand-in
// for piweb-worker.service), then idles like a long-lived Pi process.
import { launchWorker } from '../../worker-launch.ts';

const [root, socket] = process.argv.slice(2);
const launched = await launchWorker({
  root, python: `${root}/.venv/bin/python`, worker: `${root}/worker.py`, socket, screen: '1366x768x24',
});
process.stdout.write(`${JSON.stringify({ mode: launched.mode, unit: launched.unit ?? null })}\n`);
setInterval(() => {}, 1 << 30);
