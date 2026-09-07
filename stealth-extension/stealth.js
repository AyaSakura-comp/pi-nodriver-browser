// High-Fidelity Fingerprint Spoofing & Stealth Injections
(function() {
  'use strict';

  // 1. Wipe navigator.webdriver
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined,
      configurable: true
    });
  } catch (e) {}

  // 2. Realistic window.chrome object
  if (!window.chrome) {
    window.chrome = {};
  }
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: function() {},
      sendMessage: function() {},
      onMessage: { addListener: function() {} }
    };
  }
  if (!window.chrome.csi) {
    window.chrome.csi = function() {
      return { startE: Date.now(), onloadT: Date.now() + 100, pageT: 100, tran: 15 };
    };
  }
  if (!window.chrome.loadTimes) {
    window.chrome.loadTimes = function() {
      return {
        requestTime: Date.now() / 1000,
        startLoadTime: Date.now() / 1000,
        commitLoadTime: Date.now() / 1000 + 0.1,
        finishDocumentLoadTime: Date.now() / 1000 + 0.2,
        finishLoadTime: Date.now() / 1000 + 0.3,
        firstPaintTime: Date.now() / 1000 + 0.15,
        firstPaintAfterLoadTime: 0,
        navigationType: 'Other',
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
        npnNegotiatedProtocol: 'h2',
        wasAlternateProtocolAvailable: false,
        connectionInfo: 'h2'
      };
    };
  }

  // 3. Preserve native WebGL and plugin fingerprints. Spoofing a desktop
  // NVIDIA/Windows renderer or legacy desktop plugins creates contradictions
  // that are easier to detect than Chrome's real Linux values.

  // 4. Preferred languages
  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['zh-TW', 'zh', 'en-US', 'en'],
      configurable: true
    });
  } catch (e) {}

  // 5. Permissions Query Spoofing (Notifications)
  if (navigator.permissions && navigator.permissions.query) {
    const origQuery = navigator.permissions.query;
    navigator.permissions.query = function(parameters) {
      if (parameters && parameters.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission === 'denied' ? 'prompt' : Notification.permission });
      }
      return origQuery.apply(this, arguments);
    };
  }

})();
