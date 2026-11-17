import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Button } from './Button';

describe('Button', () => {
  it('shows its label', () => {
    render(<Button label="Save" />);
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
  });

  it.skip('is disabled while saving', () => {
    render(<Button label="Save" saving />);
    expect(screen.getByRole('button')).toHaveProperty('disabled', true);
  });
});
