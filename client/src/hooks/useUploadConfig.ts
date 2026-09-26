import { useEffect, useState } from 'react';
import { fetchUploadConfig, type UploadConfig } from '@/utils/uploadConfig';

/** Loads the server's upload settings once; `config` is null until they arrive (or `error` is set). */
export const useUploadConfig = (): { config: UploadConfig | null; error: string | null } => {
  const [config, setConfig] = useState<UploadConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    fetchUploadConfig()
      .then((loaded) => active && setConfig(loaded))
      .catch((err: unknown) => active && setError(err instanceof Error ? err.message : 'Could not load settings.'));
    return () => {
      active = false;
    };
  }, []);

  return { config, error };
};
