import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import StoredUploadsTable from '@/components/ui/uploadFile/StoredUploadsTable';

const doc = (id: string, title: string, fileName: string) => ({
  document_id: id, title, file_name: fileName, file_ext: 'docx', size_bytes: 36597,
  created_at: '2026-09-27T10:00:00+00:00',
});

describe('StoredUploadsTable — the Library table', () => {
  it('lists documents in the order given (the API sends newest first) with a download per row', async () => {
    const onDownload = vi.fn();
    render(<StoredUploadsTable documents={[doc('d2', 'Newer', 'newer.docx'), doc('d1', 'Older', 'older.docx')]}
                               onDownload={onDownload} />);
    expect(screen.getByText('Library (2)')).toBeInTheDocument();
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell')[0].textContent)).toEqual(['Newer', 'Older']);
    expect(within(rows[0]).getByText('newer.docx')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Download Older' }));
    expect(onDownload).toHaveBeenCalledWith('d1');
  });

  it('says when the library is empty, and shows loading and errors', () => {
    const { rerender } = render(<StoredUploadsTable documents={[]} onDownload={vi.fn()} />);
    expect(screen.getByText(/No documents in the library yet/)).toBeInTheDocument();
    rerender(<StoredUploadsTable documents={[]} isLoading onDownload={vi.fn()} />);
    expect(screen.getByText('Loading…')).toBeInTheDocument();
    rerender(<StoredUploadsTable documents={[]} error="Failed to load the library (HTTP 500)." onDownload={vi.fn()} />);
    expect(screen.getByText('Failed to load the library (HTTP 500).')).toBeInTheDocument();
  });
});
