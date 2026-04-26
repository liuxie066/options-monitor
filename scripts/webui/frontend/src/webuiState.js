export function nowId() {
  return `${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

export function buildStrategySidePayload(strategyFields, source) {
  return Object.fromEntries(
    strategyFields
      .filter(([key]) => String(source[key] || '').trim() !== '')
      .map(([key]) => [key, Number(source[key])]),
  );
}

export function filterRowsByKeyword(rows, keyword) {
  const q = String(keyword || '').trim().toUpperCase();
  return rows.filter((row) => !q || `${row.symbol || ''} ${row.name || ''}`.toUpperCase().includes(q));
}
