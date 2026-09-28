import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/testing/testUtils';
import Sidebar from '@/components/layout/Sidebar';

describe('Sidebar', () => {
  it('marks the current route as active and others as inactive', () => {
    renderWithProviders(<Sidebar />, { route: '/dashboard/content-library' });

    expect(screen.getByRole('link', { name: 'Content Library' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute('aria-current');
  });

  it('renders all primary nav links and the signed-in user', () => {
    renderWithProviders(<Sidebar />, { route: '/dashboard' });

    expect(screen.getByRole('link', { name: 'Dashboard' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(screen.queryByRole('link', { name: /poc 5/i })).not.toBeInTheDocument(); // removed with the POC 5 page
    expect(screen.getByRole('link', { name: 'Content Library' })).toBeInTheDocument();
    expect(screen.getByText('John Doe')).toBeInTheDocument();
    expect(screen.getByText('clinic-lead@mytonomy.com')).toBeInTheDocument();
  });
});
