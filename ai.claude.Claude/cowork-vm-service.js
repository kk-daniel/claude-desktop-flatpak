'use strict';

// Minimal Linux placeholder for Claude's cowork VM service entrypoint. The
// upstream app forks this path when cowork features are enabled; keeping a real
// script here prevents module resolution failures even when the full VM backend
// is unavailable inside Flatpak.
setInterval(() => {}, 1 << 30);
