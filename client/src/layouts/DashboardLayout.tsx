import { Outlet } from 'react-router-dom';
import Sidebar from '@/components/layout/Sidebar';
import GlobalUploadWidget from '@/components/ui/uploadFile/GlobalUploadWidget';
import { UploadQueueProvider } from '@/context/UploadQueueContext';

/**
 * UploadQueueProvider lives here rather than in a page component: this
 * layout stays mounted across every /dashboard/* route (the router only
 * swaps the <Outlet> content), so an upload started on one page keeps
 * running — and stays visible in GlobalUploadWidget — after navigating to
 * another.
 */
const DashboardLayout = () => {
  return (
    <UploadQueueProvider>
      <div className="flex min-h-screen bg-surface">
        <Sidebar />
        <div className="flex flex-1 flex-col">
          <Outlet />
        </div>
      </div>
      <GlobalUploadWidget />
    </UploadQueueProvider>
  );
};

export default DashboardLayout;
