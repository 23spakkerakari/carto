import { Navigate, Route, Routes } from 'react-router';

import { AuditPage } from './screens/audit/AuditPage';
import { EntitiesPage } from './screens/entities/EntitiesPage';
import { FieldsPage } from './screens/fields/FieldsPage';
import { ReviewPage } from './screens/review/ReviewPage';
import { SourceDetailPage } from './screens/sources/SourceDetailPage';
import { SourcesPage } from './screens/sources/SourcesPage';
import { SystemDetailPage } from './screens/systems/SystemDetailPage';
import { SystemsPage } from './screens/systems/SystemsPage';
import { Shell } from './shell/Shell';
import { PageHeader } from './ui/primitives';

function NotFound() {
  return (
    <>
      <PageHeader title="Not found" kicker="carto" />
      <p>There is no page at this address.</p>
    </>
  );
}

export function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/map/review" replace />} />
        <Route path="map/review" element={<ReviewPage />} />
        <Route path="map/entities" element={<EntitiesPage />} />
        <Route path="systems" element={<SystemsPage />} />
        <Route path="systems/:id" element={<SystemDetailPage />} />
        <Route path="setup/sources" element={<SourcesPage />} />
        <Route path="setup/sources/:id" element={<SourceDetailPage />} />
        <Route path="setup/fields" element={<FieldsPage />} />
        <Route path="admin/audit" element={<AuditPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
