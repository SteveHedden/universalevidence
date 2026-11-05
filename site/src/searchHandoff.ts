import type { SelectedAxes } from './api/client';

export function initialSearchAxes(search: string): SelectedAxes {
  const params = new URLSearchParams(search);
  const axes: SelectedAxes = {};
  for (const [axis, vocabulary] of [['state', 'states'], ['intervention', 'interventions']] as const) {
    const uri = params.get(axis) || '';
    const prefix = `https://universalevidence.com/vocab/${vocabulary}/`;
    if (uri.startsWith(prefix) && /^[A-Za-z0-9_-]+$/.test(uri.slice(prefix.length))) {
      axes[axis] = { uri, label: (params.get(`${axis}_label`) || uri.slice(prefix.length)).slice(0, 240) };
    }
  }
  return Object.keys(axes).length ? axes : { state: { uri: 'https://universalevidence.com/vocab/states/Malaria', label: 'Malaria' } };
}
