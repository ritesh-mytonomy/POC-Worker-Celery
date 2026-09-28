import { cn } from '@/utils/cn';
import type { StatusLabel } from '@/utils/statusMapping';

const TONE_CLASS: Record<StatusLabel['tone'], string> = {
  progress: 'text-muted',
  success: 'text-success',
  warning: 'text-amber-600',
  danger: 'text-danger',
  muted: 'text-muted',
};

/** The file's processing status from the server (design.md §3), under its upload line. */
const ServerStatusLine = ({ status }: { status: StatusLabel }) => (
  <p className={cn('mt-xs truncate text-xs font-medium', TONE_CLASS[status.tone])} title={status.label}>
    {status.label}
  </p>
);

export default ServerStatusLine;
