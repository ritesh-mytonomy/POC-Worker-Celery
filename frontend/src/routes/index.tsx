import { createBrowserRouter, Navigate } from 'react-router-dom';
import { publicRoutes } from '@/routes/public.routes';
import { privateRoutes } from '@/routes/private.routes';

// The dev-only /playground route is removed: src/playground/ is git-ignored in the Upload POC, so from a clean
// checkout Vite could not resolve it and served a blank page (upload-ingest-merge 6.4).
export const router = createBrowserRouter([
  {
    path: '/',
    element: <Navigate to="/dashboard" replace />,
  },
  ...publicRoutes,
  ...privateRoutes,
  {
    path: '*',
    element: <Navigate to="/dashboard" replace />,
  },
]);
