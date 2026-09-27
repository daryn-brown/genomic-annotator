export const PALETTE = [
  "#d4ed74", "#c8e8c1", "#b6dfd0", "#b5dde8", "#bccbee",
  "#c9c1e6", "#dec7e8", "#efcce1", "#f2cbbd", "#ecdb9e",
];

export function formatNumber(value) {
  return new Intl.NumberFormat("en-US").format(value);
}

export function polar(cx, cy, radius, angle) {
  const radians = (angle - 90) * Math.PI / 180;
  return [cx + radius * Math.cos(radians), cy + radius * Math.sin(radians)];
}

export function arcPath(cx, cy, inner, outer, start, end) {
  const a = polar(cx, cy, outer, start);
  const b = polar(cx, cy, outer, end);
  const c = polar(cx, cy, inner, end);
  const d = polar(cx, cy, inner, start);
  const large = end - start > 180 ? 1 : 0;
  return `M ${a} A ${outer} ${outer} 0 ${large} 1 ${b} L ${c} A ${inner} ${inner} 0 ${large} 0 ${d} Z`;
}

export function chromosomeSegments(chromosomes) {
  if (chromosomes.length === 0) return [];
  const step = 360 / chromosomes.length;
  const gap = Math.min(2.4, step * 0.12);
  return chromosomes.map((chromosome, index) => ({
    ...chromosome,
    start: index * step + gap / 2,
    end: (index + 1) * step - gap / 2,
    color: PALETTE[index % PALETTE.length],
  }));
}

export function positionAngle(segment, position) {
  const fraction = Math.max(0, Math.min(1, (position - 1) / Math.max(1, segment.max_position - 1)));
  return segment.start + fraction * (segment.end - segment.start);
}

export function pageLabel(offset, count, total) {
  return total === 0 ? "0 matching variants" :
    `${formatNumber(offset + 1)} - ${formatNumber(offset + count)} of ${formatNumber(total)}`;
}

export function sourceLabel(source) {
  return {
    cache: "Local cache",
    myvariant: "MyVariant.info",
    none: "Not available",
    synthetic: "Synthetic demo",
  }[source] ?? source;
}

export function shortStatus(row) {
  return {
    annotated: row.clinical_significance || "Annotation available",
    offline_cache_miss: "No cached annotation",
    unsupported_id: "Local-only identifier",
    no_details: "Details unavailable",
    not_found: "RSID not found",
    fetch_failed: "Lookup incomplete",
  }[row.annotation_status] ?? row.annotation_status;
}
