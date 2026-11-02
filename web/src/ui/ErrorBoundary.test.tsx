import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ErrorBoundary } from './ErrorBoundary';

const SERVER_TEXT = '<img src=x onerror=alert(1)> customer secret 4111';

function Throws({ what }: { what: unknown }): never {
  throw what;
}

describe('ErrorBoundary', () => {
  it('renders the text-only fallback and nothing of the thrown message', () => {
    // React reports the caught error on console.error; keep the test output clean.
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { container } = render(
      <ErrorBoundary>
        <Throws what={new TypeError(SERVER_TEXT)} />
      </ErrorBoundary>,
    );
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('The console stopped');
    expect(screen.getByRole('link', { name: 'Reload the console' })).toHaveAttribute('href', '/');
    expect(container.textContent).toContain('(TypeError)');
    expect(container.textContent).not.toContain('customer secret');
    expect(container.textContent).not.toContain('4111');
    expect(container.textContent).not.toContain('onerror');
    expect(container.querySelector('img')).toBeNull();
  });

  it('names a non-error value by its type only', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { container } = render(
      <ErrorBoundary>
        <Throws what={{ message: SERVER_TEXT }} />
      </ErrorBoundary>,
    );
    expect(container.textContent).toContain('(object)');
    expect(container.textContent).not.toContain('customer secret');
  });

  it('renders its children while nothing throws', () => {
    render(
      <ErrorBoundary>
        <p>fine</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText('fine')).toBeInTheDocument();
    expect(screen.queryByRole('heading')).toBeNull();
  });
});
