import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { RejectForm } from './RejectForm';

describe('RejectForm', () => {
  it('requires a reason before it submits', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<RejectForm onSubmit={onSubmit} onCancel={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Reject link' }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a reason before rejecting.');
    expect(screen.getByRole('radiogroup')).toHaveAttribute('aria-invalid', 'true');

    await user.click(screen.getByRole('radio', { name: /Coincidence/ }));
    await user.type(screen.getByLabelText(/Note/), '  Same amounts by chance.  ');
    await user.click(screen.getByRole('button', { name: 'Reject link' }));
    expect(onSubmit).toHaveBeenCalledWith({
      reason: 'coincidence',
      note: 'Same amounts by chance.',
    });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('offers exactly the three spec 9.6 reasons and cancels', async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<RejectForm onSubmit={vi.fn()} onCancel={onCancel} />);
    const radios = screen.getAllByRole('radio');
    expect(radios.map((r) => r.getAttribute('value'))).toEqual([
      'different_meaning',
      'coincidence',
      'wrong_direction',
    ]);
    expect(screen.getByLabelText(/Note/)).toHaveAttribute('maxlength', '512');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalled();
  });

  it('disables the buttons while busy and shows the error line', () => {
    render(<RejectForm onSubmit={vi.fn()} onCancel={vi.fn()} busy error={new Error('x')} />);
    expect(screen.getByRole('button', { name: 'Rejecting…' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong.');
  });
});
