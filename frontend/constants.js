// ── Review status options ─────────────────────────────────────────────────────
const REVIEW_OPTIONS = [
  { value: '',         label: 'A revisar',    cls: 'bg-gray-100 text-gray-600' },
  { value: 'en_duda',     label: 'En duda',     cls: 'bg-yellow-100 text-yellow-800' },
  { value: 'interesante', label: 'Interesante',  cls: 'bg-blue-100 text-blue-800' },
  { value: 'descartada',  label: 'Descartada',   cls: 'bg-orange-100 text-orange-800' },
  { value: 'contactar',   label: 'Contactar',    cls: 'bg-green-100 text-green-800' },
];

// ── Property status options ──────────────────────────────────────────────────
const STATUS_OPTIONS = [
  { value: 'active',  label: 'Activas',   cls: 'bg-green-100 text-green-800' },
  { value: 'removed', label: 'Removidas', cls: 'bg-red-100 text-red-800' },
];

// ── Search engine labels ─────────────────────────────────────────────────────
const ENGINE_LABELS = {
  zonaprop: 'Zonaprop',
  argenprop: 'Argenprop',
  mercadolibre: 'MercadoLibre',
  remax: 'Remax',
};

// ── Review status CSS classes ────────────────────────────────────────────────
const REVIEW_CLASSES = {
  '':            'bg-gray-100 text-gray-600',
  en_duda:       'bg-yellow-100 text-yellow-800',
  interesante:   'bg-blue-100 text-blue-800',
  descartada:    'bg-orange-100 text-orange-800',
  contactar:     'bg-green-100 text-green-800',
};

// ── Map pin colors by review status ──────────────────────────────────────────
const PIN_COLORS = {
  en_duda:     '#f59e0b',
  interesante: '#3b82f6',
  descartada:  '#f97316',
  contactar:   '#22c55e',
  removed:     '#cbd5e1',
  default:     '#64748b',
};

// ── Map legend ───────────────────────────────────────────────────────────────
const MAP_LEGEND = [
  { label: 'Sin revisión', color: '#64748b' },
  { label: 'En duda',      color: '#f59e0b' },
  { label: 'Interesante',  color: '#3b82f6' },
  { label: 'Descartada',   color: '#f97316' },
  { label: 'Contactar',    color: '#22c55e' },
  { label: 'Removida',     color: '#cbd5e1' },
];

// ── API helper — all fetch calls go through here ─────────────────────────────
async function api(method, path, body = null) {
  const opts = { method, headers: {} };
  if (body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api${path}`, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return res.json();
}
