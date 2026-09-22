# One-shot image search: Pi + Qwen verification

## Scope and publication status

This report records a live test on 2026-09-23 (Asia/Taipei) of the **locally
installed working-tree implementation**, not the committed release at the time
of the test. The starting Git HEAD was `531aa73`; the working tree included
uncommitted browser changes and the new Lens/image-search implementation.
Publishing this report does **not** imply that these tools are available in a
checkout of that HEAD or that the implementation has been published.

The test used the actual Pi CLI and the installed extension, not direct Python
worker calls or a mocked model. Browser state was isolated with a private Unix
socket, Chrome profile, download directory and Xvfb process. No shared browser,
PiWeb or model service was restarted for the test.

## Intended interface

The local extension exposes a dedicated tool:

```text
image_search({"path":"/absolute/path/to/image.png"})
```

The equivalent browser action is:

```text
browser({"command":"image-search '/absolute/path/to/image.png'"})
```

A single call validates the local image, opens an internally verified Google
Images entry in desktop mode, prepares its camera dialog once, uploads once and
returns up to 20 title/URL/snippet/ref candidates. The agent does not need to
search for the Google Images URL, change browser mode or manually click upload
controls first. The previous session mode is restored after the operation.

The agent may then open a matching candidate using
`browser({"command":"google-lens-select @lens-…"})`. Selection is separate from
search: the tool does not automatically select the first result.

Natural prompts such as 「搜圖」、「找原圖」 or 「找同款」 with an attached image
are guided toward `image_search`. The exact local attachment path must be
available. Images are sent to Google; ambiguous or private-photo requests require
clarification before external upload. Do not use this workflow to identify a
real person from their face.

## Reproduction setup

- Provider/model: `local-llama/qwen3.6-35b-q4`
- Thinking: `medium`
- Extension: installed `nodriver-browser/index.ts`
- Enabled tools: `image_search,browser,google_search,read`
- Other extensions, skills, prompt templates and project context: disabled
- Input: a non-private 200×200 red-circle PNG, attached through Pi's `@file`
  argument, with its absolute path also supplied in the prompt
- Prompt: 「搜圖，找一個相符的圖片來源。附圖的本機路徑：<fixture-path>。
  這是非私密測試圖，允許上傳搜尋。用繁體中文簡短回答。」

The prompt did **not** name Google Lens, `image_search`, a browser action or a
result ordinal. `browser` and `google_search` remained available, so choosing
the one-shot tool was not forced by removing alternative navigation tools.

Representative CLI invocation (set the three isolation variables to fresh,
private paths before starting the worker and Pi):

```bash
pi --provider local-llama --model qwen3.6-35b-q4 --thinking medium \
  --no-extensions -e "$EXTENSION/index.ts" \
  --no-skills --no-prompt-templates --no-context-files \
  --tools image_search,browser,google_search,read \
  --mode json --session-dir "$RUN/sessions" \
  -p "@$FIXTURE" "$PROMPT"
```

Required isolation variables are `PI_NODRIVER_SOCKET`, `PI_NODRIVER_PROFILE`
and `PI_NODRIVER_DOWNLOAD_DIR`. Shut down only the private test worker after
collecting the evidence; do not use the shared daemon as disposable test state.

## Observed result

The process exited successfully in **25.56 seconds**, with exactly two tool
calls:

| Step | Actual call | Observed outcome |
| --- | --- | --- |
| 1 | `image_search` with the fixture's absolute path | `status: results`, `uploadAttempted: true`, 20 candidates |
| 2 | `browser` → `google-lens-select` with the returned fourth candidate's ref | `status: selected`, destination evidence returned |

There was exactly **one `image_search` call**. There were no preliminary web
searches, manual opens, mode switches, camera clicks or repeated uploads by the
agent. The selected destination was the Chinese Wikipedia file page for
`Flag_of_Japan_(vertical).svg`.

This establishes the natural-language tool-routing and live upload/selection
flow for this fixture and environment. It is not a reliability benchmark across
multiple images, languages, identities or Google layouts.

## Important limitation: similarity is not provenance

Qwen's final answer confidently called the fixture a Japanese flag. The fixture
was merely a red circle on a white background; a visually similar flag result
does not establish its identity, author, original source or licensing.

**Workflow result: PASS. Source-attribution certainty: NOT ESTABLISHED.**

When interpreting reverse-image results:

- Say “a visually similar result” unless additional evidence establishes an
  exact match.
- A matching title or successful destination navigation does not prove the
  uploaded image originated from that page.
- Do not infer authorship, earliest publication or license from similarity.
- Distinguish exact matches, similar images and unverified hypotheses in the
  final answer. If the source cannot be established, say so.

These are interpretation requirements, not a claim that the tested Qwen answer
already followed them or that prompt wording guarantees compliance.

## Safety and verification boundaries

The local implementation stops on consent, CAPTCHA, access or login gates and
does not replay uncertain uploads. After a blocked, empty or uncertain result,
observe with `google-lens-results` without uploading, or stop; do not click
around or retry the upload. Mobile behavior and other Google locales/layouts
were not covered by this CLI test.

Local automated validation before the CLI run reported:

- 12 new one-shot image-search tests passed.
- Full working-tree suite: 490 tests, OK, with 90 browser integration tests
  skipped. This is **not** 490 executed passing tests.
- Python compilation and `git diff --check` passed.

The new cases cover tool registration, literal path quoting, single dispatch,
no transport replay, readiness, gate handling, timeout/cancellation, mode
restoration and pending-upload accounting. These counts describe the local
working tree, not a clean checkout of the starting HEAD.

The raw JSON event stream and a machine-readable summary were retained locally
for audit. They are not embedded here because raw browser URLs can contain
session-specific identifiers. No user-provided private photo was used or
published in this verification.
