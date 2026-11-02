import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// jsdom has no ResizeObserver; Radix primitives observe their own size.
class ResizeObserverStub {
  observe(): void {
    // jsdom never lays out, so there is nothing to observe.
  }
  unobserve(): void {
    // see observe
  }
  disconnect(): void {
    // see observe
  }
}
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = ResizeObserverStub;
}

afterEach(() => {
  cleanup();
});
