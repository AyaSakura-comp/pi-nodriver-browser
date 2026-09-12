# Bad UI Seven-Level Benchmark

This suite measures end-to-end Pi agent behavior on deliberately awkward browser controls rather than testing browser APIs in isolation. The 390px fixture advances through seven sequential targets:

1. low-contrast fake-ad close button (`adClose`)
2. custom checkbox (`checkbox`)
3. native delivery dropdown (`dropdown`)
4. cart stepper (`cartPlus`)
5. tiny low-contrast plus button (`tinyPlus`)
6. non-semantic toggle (`toggle`)
7. input-like coupon control (`coupon`)

## Modes

| Mode | Policy | Extra environment |
|---|---|---|
| `forced-omni` | Worker allows only `open`, `vision-mark omni`, and exact-center `vision-click` | vision-only + forced-Omni policy |
| `hybrid` | CDP/DOM semantic actions first, Omni fallback; manual marker flow rejected | Omni fallback + hybrid policy |
| `semantic-manual` | CDP/DOM first, ordinary marked-image fallback; Omni rejected | manual fallback + semantic-manual policy |

Prompts live in `prompts/` and use `{{FIXTURE_URL}}`; `run.py` resolves that placeholder to the checked-out repository's absolute `file://` URI. There are no machine-specific paths in the suite. Each trial starts this checkout's `worker.py` through a runner-owned `xvfb-run` process group with an isolated short-path `PI_NODRIVER_SOCKET` under the system temporary directory and an isolated `PI_NODRIVER_PROFILE`, explicit vision settings that override inherited values, and a worker-enforced `PI_NODRIVER_BENCHMARK_ACTION_POLICY`. This prevents the user's persistent daemon, deployed worker version, or a prior mode from contaminating the next trial.

## Run

The runner requires `pi`, this repository's current browser extension, Chrome, `xvfb-run`, and—when using Omni—the separately managed OmniParser service documented in the root README. Run `./install.sh` after changing the extension so global `pi` does not benchmark a stale deployed copy.

```bash
# Inspect commands without invoking Pi
python3 benchmarks/bad-ui/run.py --all --dry-run

# One hybrid run
python3 benchmarks/bad-ui/run.py --mode hybrid

# Five trials per mode with explicit model settings
python3 benchmarks/bad-ui/run.py --all --trials 5 \
  --provider local-llama --model qwen3.6-35b-q4 --thinking medium
```

Useful options:

- `--timeout SECONDS` sets the per-trial limit (default: 360).
- `--output-dir PATH` changes the JSONL destination.
- `--provider`, `--model`, and `--thinking` select the Pi inference configuration.
- `--worker-python PATH` selects a Python interpreter containing Nodriver dependencies; it defaults to the deployed extension's virtualenv.
- `--socket-root PATH` selects a short local Unix-socket root (default: `/tmp`); paths over the safe length fail before launch.

Results are written under `benchmarks/bad-ui/results/` by default and are intentionally ignored by Git. Each collision-resistant filename contains a UTC run ID, mode, trial number, and random suffix. Standard output streams directly to the JSONL file, standard error goes to a paired `.stderr.log`, and persistent worker diagnostics use paired `.worker.log` and `.worker.stderr.log` files, so a non-zero Pi exit, timeout, or runner interruption preserves already-written evidence without corrupting JSONL. The runner exits non-zero for process-level failures, continuously monitors the worker while Pi runs, handles shell/CI `SIGTERM` and `SIGHUP`, terminates and boundedly waits for the exact Pi and worker process groups it started, then removes the isolated profile and short socket directory. It never discovers or signals a daemon by guessed PID or command-line matching. A zero exit means the Pi process completed; inspect the final assistant event in Pi's JSONL and parse its nested assistant text as JSON, then use `success` and `completedLevels` for benchmark-quality scoring.

## Verification

The repository test suite checks the manifest against the fixture, portable paths, documentation and ignore links, prompt inventory, mode policies, isolated socket/profile plans, streamed timeout evidence, and dry-run command construction:

```bash
python3 -m unittest tests.test_bad_ui_benchmark -v
```

## Historical baseline

A single-trial Qwen benchmark on 2026-09-10 used `local-llama/qwen3.6-35b-q4` with medium thinking:

| Mode | Result |
|---|---:|
| Forced Omni | 294.23 s, 7/7 |
| Hybrid CDP + Omni | **92.03 s, 7/7** |
| CDP + ordinary vision | 300.05 s timeout, 4/7 |

These numbers are directional, not statistical medians. Use repeated `--trials` runs when comparing revisions.
