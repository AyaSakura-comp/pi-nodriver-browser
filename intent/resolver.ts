// Recovery is infrastructure-only. Never replay a possibly executed page action.
export async function ensureIntentService(
  service: string,
  probe: () => Promise<boolean>,
  startThroughHub: () => Promise<void>,
): Promise<void> {
  if (await probe()) return;
  if (service.replace(/\/$/, '') !== 'http://127.0.0.1:8011') {
    throw new Error('Intent custom endpoint unavailable; local service repair is not applicable.');
  }
  await startThroughHub();
  if (!await probe()) throw new Error('Intent service unavailable after hub repair.');
}

export function failureAdvice(message: string): { code: string; hint: string } {
  const rules: [RegExp, string, string][] = [
    [/URL_PROVENANCE/i, 'URL_PROVENANCE', 'Use the exact user/search URL or a visible candidate link. Never guess or bypass the guard.'],
    [/NEEDS_CONFIRM/i, 'USER_CONFIRMATION', 'Stop. Only confirm when the user explicitly authorized the irreversible action.'],
    [/captcha|access.denied|sign.in|login.required/i, 'ACCESS_GATE', 'Ask the user to complete the access gate. Do not bypass authentication or CAPTCHA.'],
    [/LOOP_GUARD/i, 'LOOP_GUARD', 'Inspect with observe/read. Do not reset the guard or repeatedly reopen the same origin.'],
    [/no open page/i, 'NO_PAGE', 'This session has no page yet. Open an exact authorized user/search URL; do not guess a URL or reuse another session tab.'],
    [/PICK_EXPIRED|stale|detached|target.closed|no.such.target/i, 'STALE_TARGET', 'Observe/read the current page and select a fresh visible candidate; do not replay a click.'],
    [/NO_MATCH/i, 'NO_MATCH', 'Read the current page and rephrase the target once using visible wording; then stop if absent.'],
    [/AMBIGUOUS/i, 'AMBIGUOUS', 'Choose from returned options using pick or add distinguishing constraints.'],
    [/ERR_NAME_NOT_RESOLVED|ENOTFOUND|DNS/i, 'DNS', 'Verify the exact authorized URL and network/DNS. Do not invent an alternative hostname.'],
    [/ERR_CERT|certificate|SSL/i, 'TLS', 'Report the certificate failure. Never disable TLS verification.'],
    [/429|rate.limit/i, 'RATE_LIMIT', 'Respect Retry-After; stop repeated requests.'],
    [/TAB_LIMIT|tab.cap|too.many.tabs/i, 'TAB_LIMIT', 'Close only unused tabs owned by this session; never shut down shared Chrome.'],
    [/ECONNREFUSED|fetch failed|connection|socket|FileNotFoundError/i, 'CONNECTION', 'Run browser_intent resolve to repair the local router and reconnect the worker; inspect before retrying.'],
    [/planner model .* did not answer/i, 'PLANNER_BUSY', 'The planner model was busy and no page step ran. Retry the same task once; if it fails again, answer from other sources and say so.'],
    [/timeout|timed.out/i, 'TIMEOUT', 'Execution may have happened. Observe current state before deciding whether a safe read-only retry is needed.'],
  ];
  for (const [pattern, code, hint] of rules) if (pattern.test(message)) return { code, hint };
  return { code: 'UNKNOWN', hint: 'Inspect current state and report the exact failure; no automatic replay or destructive reset.' };
}

export function streamFailure(error: unknown) {
  return {
    status: 'EXECUTION_UNCERTAIN',
    message: error instanceof Error ? error.message : String(error),
    retry_original: false,
    hint: 'Stream interrupted; the operation may already have executed. Run browser_intent resolve, then observe/read before continuing. Never replay a purchase, submission, click, task or plan blindly.',
  };
}
