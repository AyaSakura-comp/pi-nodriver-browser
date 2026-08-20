// Cloudflare Turnstile & Challenge Automatic Solver
(function() {
  'use strict';

  function simulateHumanClick(element) {
    if (!element) return;
    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2 + (Math.random() * 4 - 2);
    const y = rect.top + rect.height / 2 + (Math.random() * 4 - 2);

    const opts = { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y };
    
    element.dispatchEvent(new MouseEvent('mousemove', opts));
    element.dispatchEvent(new MouseEvent('mousedown', opts));
    element.dispatchEvent(new MouseEvent('mouseup', opts));
    element.dispatchEvent(new MouseEvent('click', opts));
  }

  function trySolveTurnstile() {
    // 1. Direct checkbox inside Cloudflare Turnstile iframe
    const checkbox = document.querySelector('input[type="checkbox"], .ctp-checkbox-label, #challenge-stage input');
    if (checkbox && !checkbox.checked) {
      simulateHumanClick(checkbox);
      return;
    }

    // 2. Cloudflare Challenge Stage wrapper button
    const stage = document.querySelector('#challenge-stage, #challenge-button, .challenge-body');
    if (stage) {
      simulateHumanClick(stage);
      return;
    }

    // 3. Shadow DOM inspection
    const allElements = document.querySelectorAll('*');
    for (let i = 0; i < allElements.length; i++) {
      const el = allElements[i];
      if (el.shadowRoot) {
        const shadowBox = el.shadowRoot.querySelector('input[type="checkbox"], #challenge-stage');
        if (shadowBox) {
          simulateHumanClick(shadowBox);
          return;
        }
      }
    }
  }

  // Continuously scan for Turnstile elements during the first 5 seconds of page load
  let attempts = 0;
  const interval = setInterval(() => {
    attempts++;
    trySolveTurnstile();
    if (attempts > 20) {
      clearInterval(interval);
    }
  }, 250);

  window.addEventListener('DOMContentLoaded', trySolveTurnstile);
  window.addEventListener('load', trySolveTurnstile);
})();
