'use strict';

if (typeof window !== 'undefined') {
  if (!('windowControlsOverlay' in navigator)) {
    Object.defineProperty(navigator, 'windowControlsOverlay', {
      value: {
        visible: true,
        getTitlebarAreaRect: () => ({ x: 0, y: 0, width: window.innerWidth, height: 32 }),
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
      },
      configurable: true,
    });
  }
}
