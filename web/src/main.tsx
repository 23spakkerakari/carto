import { QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';

import { App } from './App';
import { ApiContext, defaultApiClient, makeQueryClient } from './api/hooks';
import './styles.css';

async function enableMockApi(): Promise<void> {
  // Replaced at build time; the branch and its import vanish from the production bundle.
  if (import.meta.env.VITE_MOCK_API !== '1') return;
  const { startMockApi } = await import('./mocks/browser');
  await startMockApi();
}

const queryClient = makeQueryClient();

function mount(): void {
  const root = document.getElementById('root');
  if (!root) throw new Error('#root is missing');
  createRoot(root).render(
    <StrictMode>
      <ApiContext.Provider value={defaultApiClient}>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </ApiContext.Provider>
    </StrictMode>,
  );
}

void enableMockApi().then(mount);
