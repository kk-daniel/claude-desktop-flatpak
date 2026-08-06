'use strict';

// Explicit members ported from aaddrick/claude-desktop-debian
// scripts/claude-native-stub.js (MIT licensed — see LICENSE). The auth
// flow calls `native.AuthRequest.isAvailable()`; the previous bare-noop
// Proxy returned a plain function for `AuthRequest`, so `.isAvailable`
// was undefined and the doAuthInBrowser handler threw before it could
// fall back to the system browser — breaking Google login. Unknown
// members still fall back to noop so newer upstream builds don't crash
// on native APIs this stub doesn't know about.

const noop = () => undefined;

const KeyboardKey = Object.freeze({
  Backspace: 43, Tab: 280, Enter: 261, Shift: 272, Control: 61, Alt: 40,
  CapsLock: 56, Escape: 85, Space: 276, PageUp: 251, PageDown: 250,
  End: 83, Home: 154, LeftArrow: 175, UpArrow: 282, RightArrow: 262,
  DownArrow: 81, Delete: 79, Meta: 187,
});

// Helper: get the focused BrowserWindow (lazy-loaded to avoid circular
// deps). Filters destroyed windows from the fallback. isVisible() is
// intentionally NOT checked — flashFrame() must work on minimized
// (non-visible) windows, which is its primary use case.
function getWindow() {
  try {
    const { BrowserWindow } = require('electron');
    const focused = BrowserWindow.getFocusedWindow();
    if (focused) return focused;
    const win = BrowserWindow.getAllWindows().find((w) => !w.isDestroyed());
    return win || null;
  } catch (e) {
    console.warn('[Claude Native Stub] getWindow() failed:', e);
    return null;
  }
}

// Not available on Linux; isAvailable() === false routes the auth flow
// to its system-browser fallback.
class AuthRequest {
  static isAvailable() {
    return false;
  }

  async start(_url, _scheme, _windowHandle) {
    throw new Error('AuthRequest not available on Linux');
  }

  cancel() {}
}

const native = {
  getWindowsVersion: () => '10.0.0',

  // Functional on Linux via Electron's native support
  getIsMaximized: () => {
    const win = getWindow();
    return win ? win.isMaximized() : false;
  },
  flashFrame: (flash) => {
    const win = getWindow();
    if (win) win.flashFrame(typeof flash === 'boolean' ? flash : true);
  },
  clearFlashFrame: () => {
    const win = getWindow();
    if (win) win.flashFrame(false);
  },
  setProgressBar: (progress) => {
    const win = getWindow();
    if (win && typeof progress === 'number') {
      win.setProgressBar(Math.max(0, Math.min(1, progress)));
    }
  },
  clearProgressBar: () => {
    const win = getWindow();
    if (win) win.setProgressBar(-1);
  },

  KeyboardKey,
  AuthRequest,
};

module.exports = new Proxy(native, {
  get(target, prop) {
    if (prop === '__esModule') {
      return false;
    }
    if (prop in target) {
      return target[prop];
    }
    return noop;
  },
});
