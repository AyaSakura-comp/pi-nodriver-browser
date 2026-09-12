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
| `forced-omni` | Only `open`, `vision-mark omni`, and exact-center `vision-click` | `PI_NODRIVER_VISION_ONLY=1` |
| `hybrid` | CDP/DOM semantic actions first, Omni fallback | none |
| `semantic-manual` | CDP/DOM first, ordinary marked-image fallback; Omni prohibited | `PI_NODRIVER_VISION_FALLBACK=manual` |

Prompts live in `prompts/` and use `{{FIXTURE_URL}}`; `run.py` resolves that placeholder to the checked-out repository's absolute `file://` URI. There are no machine-specific paths in the suite. Each trial receives an isolated socket and Chrome profile, explicit vision settings that override inherited values, and a worker-enforced `PI_NODRIVER_BENCHMARK_ACTION_POLICY`. This prevents a persistent daemon or a prior mode from contaminating the next trial.

## Run

The runner requires `pi`, the browser extension, Chrome/Xvfb, and—when using Omni—the separately managed OmniParser service documented in the root README.

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

Results are written under `benchmarks/bad-ui/results/` by default and are intentionally ignored by Git. Each collision-resistant filename contains a UTC run ID, mode, trial number, and random suffix. Standard output streams directly to the JSONL file and standard error goes to a paired `.stderr.log`, so a non-zero Pi exit, timeout, or runner interruption preserves already-written evidence without corrupting JSONL. The runner exits non-zero for failed trials and safely terminates only the daemon whose command line matches that trial's unique socket.

## Verification

The repository test suite checks the level manifest, portable paths, prompt inventory, mode policies, and dry-run command construction:

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
