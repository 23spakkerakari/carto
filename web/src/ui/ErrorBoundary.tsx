import { Component, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  /** The constructor name of what was thrown, or null while nothing has failed. */
  failed: string | null;
}

/** Names the thrown value by its class only. Never its message: an `ApiError`'s message is the
 * problem title the server sent, and anything else may carry customer data (spec 13). */
function kindOf(value: unknown): string {
  if (value instanceof Error) return value.constructor.name || 'Error';
  return typeof value;
}

/** Catches a render-time throw anywhere below it and shows a text-only fallback with a reload
 * link instead of a blank page. Nothing of the thrown value reaches the DOM except the name of
 * its class. */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { failed: null };

  static getDerivedStateFromError(error: unknown): State {
    return { failed: kindOf(error) };
  }

  // No componentDidCatch: React reports the error itself, and nothing here echoes a message
  // that may carry server data.
  override render(): ReactNode {
    if (this.state.failed === null) return this.props.children;
    return (
      <main className="mx-auto max-w-3xl p-8" aria-labelledby="error-boundary-title">
        <h1 id="error-boundary-title" className="font-cond text-[26px] leading-8 font-semibold">
          The console stopped
        </h1>
        <p className="mt-2">
          Something went wrong while drawing this page ({this.state.failed}). Nothing was saved or
          changed by this.
        </p>
        <p className="mt-2">
          <a href="/" className="underline underline-offset-2">
            Reload the console
          </a>
        </p>
      </main>
    );
  }
}
