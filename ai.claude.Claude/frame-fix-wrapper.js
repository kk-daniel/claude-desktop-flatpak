'use strict';

// require('electron') interception + BrowserWindowWithFrame subclass derived
// from aaddrick/claude-desktop-debian@7882635 (speleoalex, "Add native window
// decorations support for Linux"). MIT licensed — see LICENSE.

const Module = require('module');
const originalRequire = Module.prototype.require;

function titlebarStyle() {
  return String(process.env.CLAUDE_TITLEBAR_STYLE || 'hybrid').toLowerCase();
}

let PatchedBrowserWindow = null;

Module.prototype.require = function patchedRequire(id) {
  const result = originalRequire.apply(this, arguments);

  if (id !== 'electron' || !result || !result.BrowserWindow) {
    return result;
  }

  if (!PatchedBrowserWindow) {
    const OriginalBrowserWindow = result.BrowserWindow;
    PatchedBrowserWindow = class BrowserWindowWithFrame extends OriginalBrowserWindow {
      constructor(options) {
        if (process.platform === 'linux') {
          options = options || {};
          const style = titlebarStyle();
          if (style === 'hidden') {
            options.frame = false;
            options.titleBarStyle = 'hidden';
            options.titleBarOverlay = true;
          } else {
            options.frame = true;
            delete options.titleBarStyle;
            delete options.titleBarOverlay;
          }
        }
        super(options);
      }
    };

    for (const key of Object.getOwnPropertyNames(OriginalBrowserWindow)) {
      if (key === 'prototype' || key === 'length' || key === 'name') continue;
      const descriptor = Object.getOwnPropertyDescriptor(OriginalBrowserWindow, key);
      if (!descriptor) continue;
      try {
        Object.defineProperty(PatchedBrowserWindow, key, descriptor);
      } catch {
        // skip non-configurable mismatches
      }
    }
  }

  return new Proxy(result, {
    get(target, prop, receiver) {
      if (prop === 'BrowserWindow') return PatchedBrowserWindow;
      return Reflect.get(target, prop, receiver);
    },
  });
};
