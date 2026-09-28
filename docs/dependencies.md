# Pinned dependent projects

The superproject records dependencies as **Git submodules**, not copied source
or automatically started services. Each gitlink is the authoritative exact
commit. Branch hints apply only to an intentional `update --remote` operation;
ordinary checkout follows the pinned commit, never the latest branch tip.

| Path | Repository | Update branch | Initial pin | Access / role |
|---|---|---|---|---|
| `dependencies/omniparser` | `AyaSakura-comp/OmniParser` | `master` | `f4e53d5` | Public; YOLO detector `/parse`, normally localhost:8012 |
| `dependencies/laya` | `AyaSakura-comp/laya` | `laya` | `e676305` | Public; Laya model server `/v1/systemone`, normally localhost:8000 |
| `dependencies/laya-browser-intent` | `AyaSakura-comp/laya-browser-intent` | `laya` | `28a9b73` | **Private**; natural-language intent API, normally localhost:8011, plus separate Pi extension |
| `dependencies/xvfb-streaming` | `AyaSakura-comp/xvfb-streaming` | `master` | `e97f706` | **Private**; optional Chrome/Xvfb screen streaming |

Laya's model-server repository and Laya Browser Intent are different dependencies.
The `laya` branch is intentional; their default branches are not the deployed
integration branches. OmniParser points to the user's fork containing the INT8
CPU deployment code, not the unchanged Microsoft upstream tree.

## Clone and initialize

Public/basic setup (no private access required):

```sh
git clone https://github.com/AyaSakura-comp/pi-nodriver-browser.git
cd pi-nodriver-browser
git submodule update --init -- dependencies/omniparser dependencies/laya
```

Authenticated full setup, for an account with access to both private repos:

```sh
gh auth login
gh auth setup-git
git submodule update --init --recursive
git submodule status --recursive
```

Alternatively use `git clone --recurse-submodules` **after** configuring access.
All `.gitmodules` URLs are credential-free HTTPS. Never embed a token in a URL,
commit auth headers, or make a private repository public to fix an auth error.
A public clone without private access cannot initialize all four dependencies;
this does not prevent the core semantic browser from being used.

For an existing checkout after pulling:

```sh
git submodule sync --recursive
git submodule update --init --recursive
```

Avoid `--remote` in normal installation: it changes the tested versions.
Shallow single-branch clones may omit the custom `laya` branches; fetch the
named branch explicitly or use normal non-shallow submodule initialization.

## Deliberate dependency upgrades

```sh
# Inspect local changes in each dependency first; do not discard them.
git submodule foreach 'git status --short'
git submodule update --remote -- dependencies/omniparser
# Review the new commit and run the relevant tests before recording it.
git diff --submodule=log -- dependencies/omniparser
git add dependencies/omniparser
git commit -m "chore(deps): update OmniParser pin"
```

When editing a submodule, create a branch in it, commit and **push that child
commit first**, then record/push the changed parent gitlink. Otherwise fresh
clones cannot fetch the referenced commit. Uncommitted changes in a separate
`~/src` working copy are never captured by a submodule pin.

## Provisioning is explicit

Fetching source does **not** install environments, download weights, copy a Pi
extension, create credentials, update systemd units, start Docker, or expose
ports. The existing browser installer keeps its no-external-service-install
boundary. It does not bundle submodule contents into the installed extension.

- OmniParser: follow `dependencies/omniparser/docs/CPU_INT8_INSTALL.md`. The
  opt-in INT8 CPU version is lossy and did not meet cosine >=0.99 in the 20-site
  comparison. Core PyTorch/GPU inference remains available as a rollback.
- Laya model server: consult its README and `examples/server.py --help`; provision
  its weights separately. Use localhost binding. Laya is a model dependency,
  not bundled weights and not TypeSafe's hosted Jev model.
- Laya Browser Intent: consult its README. Its separately loaded
  `pi-extension/browser-intent.ts` provides `browser_intent`; do not load duplicate
  extension copies. Configure its Laya URL and browser socket for your host.
- Xvfb Streaming: consult its README/compose setup. Supply your own ignored
  environment and access configuration; the private repo contains committed
  source only, not this host's `.env` or `.env.bak-before-autosize`.

Existing running services on the original host continue to use their separate
working copies. No running service paths, sockets, environments or settings were
migrated by adding submodules. Migration to these source locations is a separate
explicit deployment step via the host's restart-service procedures.

Other dependencies remain in their proper package/runtime layer: Python/nodriver
and supporting libraries in `requirements.txt`, Chrome/Xvfb system packages,
4get and model APIs as separately configured endpoints, and model files outside
Git. They are not arbitrary source repos to add as submodules. The optional
Xvfb host restart scripts also remain host-specific, not copied from secret-
bearing configuration directories.

## Offline checks

```sh
.venv/bin/python -m unittest discover -s tests -p test_dependency_submodules.py -v
git diff --cached --check
git submodule status
```

These check metadata and staged gitlinks without network access, credentials,
private submodule initialization, service operations or model loads.
