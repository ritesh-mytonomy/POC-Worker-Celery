export function isDuplicateError(errors?: string[]): boolean {
  return Boolean(errors?.some((error) => error.toLowerCase().includes('duplicate')));
}

export function isDuplicateCloudError(message?: string): boolean {
  return Boolean(message?.toLowerCase().includes('duplicate'));
}

export function duplicateQueueMessage(filename: string): string {
  return `Duplicate file — ${filename} is already in the upload queue.`;
}

export function isSameFile(
  a: { name: string; size: number },
  b: { name: string; size: number },
): boolean {
  return a.name.toLowerCase() === b.name.toLowerCase() && a.size === b.size;
}

export function findQueueDuplicate(
  candidates: { id: string; name: string; size: number }[],
  file: { name: string; size: number },
  excludeId?: string,
): boolean {
  return candidates.some(
    (item) => item.id !== excludeId && isSameFile(item, file),
  );
}
