import JSZip from 'jszip';
import { describe, expect, it } from 'vitest';
import {
  acceptedFileInput,
  getUploadableExtension,
  unsupportedFormatMessage,
  validateUploadFile,
} from '@/utils/contentValidation';
import type { UploadConfig } from '@/utils/uploadConfig';

// A docx-only config (the default before D2 allowed PDFs): .docx, and .zip of .docx, one subfolder at most.
const CONFIG: UploadConfig = {
  allowedTopLevelExt: ['docx', 'zip'],
  allowedZipEntryExt: ['docx'],
  maxUploadBytes: 5 * 1024 ** 3,
  maxZipFolderDepth: 1,
};

const zipOf = async (names: string[]): Promise<File> => {
  const zip = new JSZip();
  for (const name of names) zip.file(name, 'content');
  return new File([await zip.generateAsync({ type: 'arraybuffer' })], 'drop.zip', { type: 'application/zip' });
};

describe('allowed types come from the server config', () => {
  it('accepts only the configured top-level types', () => {
    expect(getUploadableExtension('Guide.DOCX', CONFIG)).toBe('docx');
    expect(getUploadableExtension('bundle.zip', CONFIG)).toBe('zip');
    expect(getUploadableExtension('notes.pdf', CONFIG)).toBeNull();
    expect(unsupportedFormatMessage('notes.pdf', CONFIG)).toBe('.pdf is not a supported format');
    expect(acceptedFileInput(CONFIG)).toBe('.docx,.zip');
  });

  it('follows the config when Product widens it (D2)', () => {
    const wider = { ...CONFIG, allowedTopLevelExt: ['docx', 'zip', 'pdf', 'html'] };
    expect(getUploadableExtension('notes.pdf', wider)).toBe('pdf');
    expect(acceptedFileInput(wider)).toBe('.docx,.zip,.pdf,.html,.htm');
  });
});

describe('the zip check (D16)', () => {
  it('skips __MACOSX/ and hidden files silently', async () => {
    const result = await validateUploadFile(
      await zipOf(['a.docx', '__MACOSX/._a.docx', '.DS_Store', 'docs/b.docx', 'docs/.hidden.docx']),
      CONFIG,
    );
    expect(result.valid).toBe(true);
    expect(result.warnings).toEqual([]);
    expect(result.zipEntries?.map((e) => e.path)).toEqual(['a.docx', 'docs/b.docx']);
  });

  it('warns about a disallowed type and a folder nested too deeply, but still uploads', async () => {
    const result = await validateUploadFile(await zipOf(['a.docx', 'notes.pdf', 'x/y/deep.docx']), CONFIG);
    expect(result.valid).toBe(true);
    expect(result.errors).toEqual([]);
    expect(result.warnings).toEqual([
      'notes.pdf: .pdf is not supported inside a ZIP.',
      'x/y/deep.docx: Folders nested too deeply — at most one subfolder is supported',
    ]);
  });

  it('blocks a zip with no usable entry', async () => {
    const result = await validateUploadFile(await zipOf(['notes.pdf', '__MACOSX/._a.docx', 'x/y/deep.docx']), CONFIG);
    expect(result.valid).toBe(false);
    expect(result.errors).toEqual(['The ZIP file does not contain any DOCX files that can be added.']);
  });
});

describe('PDFs, with the server defaults since D2 (docx, pdf, zip of docx and pdf)', () => {
  const DEFAULTS: UploadConfig = { ...CONFIG, allowedTopLevelExt: ['docx', 'pdf', 'zip'], allowedZipEntryExt: ['docx', 'pdf'] };
  const PDF = '%PDF-1.4\n%%EOF\n';
  const zipWith = async (entries: Record<string, string>): Promise<File> => {
    const zip = new JSZip();
    for (const [name, content] of Object.entries(entries)) zip.file(name, content);
    return new File([await zip.generateAsync({ type: 'arraybuffer' })], 'drop.zip', { type: 'application/zip' });
  };

  it('offers .pdf and accepts a real PDF', async () => {
    expect(acceptedFileInput(DEFAULTS)).toBe('.docx,.pdf,.zip');
    const result = await validateUploadFile(new File([PDF], 'Discharge Guide.pdf', { type: 'application/pdf' }), DEFAULTS);
    expect(result).toEqual({ valid: true, errors: [] });
  });

  it('blocks a file named .pdf that is not a PDF', async () => {
    const result = await validateUploadFile(new File(['MZ not a pdf'], 'fake.pdf'), DEFAULTS);
    expect(result.valid).toBe(false);
    expect(result.errors).toEqual(['The PDF file is corrupted or invalid.']);
  });

  it('counts a PDF inside a zip as usable, and warns only for the rest', async () => {
    const result = await validateUploadFile(
      await zipWith({ 'notes.pdf': PDF, 'readme.txt': 'Read me.', 'broken.pdf': 'not a pdf' }), DEFAULTS);
    expect(result.valid).toBe(true);
    expect(result.warnings).toEqual([
      'readme.txt: .txt is not supported inside a ZIP.',
      'broken.pdf: The PDF file is corrupted or invalid.',
    ]);
    expect(result.zipEntries?.filter((e) => e.valid).map((e) => e.path)).toEqual(['notes.pdf']);
  });
});
