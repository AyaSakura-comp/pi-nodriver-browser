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

  // 3. WebGL Vendor & Renderer Spoofing (Desktop NVIDIA GPU)
  const patchWebGL = (proto) => {
    if (!proto) return;
    const origGetParam = proto.getParameter;
    proto.getParameter = function(param) {
      // UNMASKED_VENDOR_WEBGL (0x9245)
      if (param === 37445) {
        return 'Google Inc. (NVIDIA)';
      }
      // UNMASKED_RENDERER_WEBGL (0x9246)
      if (param === 37446) {
        return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      }
      // VENDOR (0x1F00)
      if (param === 7936) {
        return 'WebKit';
      }
      // RENDERER (0x1F01)
      if (param === 7937) {
        return 'WebKit WebGL';
      }
      return origGetParam.apply(this, arguments);
    };
  };

  if (typeof WebGLRenderingContext !== 'undefined') {
    patchWebGL(WebGLRenderingContext.prototype);
  }
  if (typeof WebGL2RenderingContext !== 'undefined') {
    patchWebGL(WebGL2RenderingContext.prototype);
  }

  // 4. Navigator Plugins & MimeTypes
  try {
    const fakePlugins = [
      { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
      { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
    ];
    Object.defineProperty(navigator, 'plugins', {
      get: () => fakePlugins,
      configurable: true
    });
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
