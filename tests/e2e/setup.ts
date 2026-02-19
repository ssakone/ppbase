/**
 * Test setup for Node.js environment.
 * Polyfills browser APIs that PocketBase SDK expects.
 *
 * Note: eventsource v4.x exports EventSource as a named export, not default.
 */
import * as EventSourceModule from 'eventsource';

// Extract EventSource class from the module
const EventSourceClass = (EventSourceModule as any).EventSource;

// Polyfill EventSource for Node.js
if (typeof globalThis.EventSource === 'undefined') {
  (globalThis as any).EventSource = EventSourceClass;
}

// Also assign to global for older environments
if (typeof (global as any).EventSource === 'undefined') {
  (global as any).EventSource = EventSourceClass;
}
