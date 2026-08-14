import { readFileSync } from 'node:fs';

const [reportPath, baselinePath, auditLevel = 'high'] = process.argv.slice(2);
const severityRank = {
  info: 0,
  low: 1,
  moderate: 2,
  high: 3,
  critical: 4,
};

if (!(auditLevel in severityRank)) {
  throw new Error(`Nivel de auditoria invalido: ${auditLevel}`);
}

function loadJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    throw new Error(`Nao foi possivel ler o relatorio npm audit em ${path}: ${error.message}`);
  }
}

function loadBaseline(path) {
  try {
    return new Set(
      readFileSync(path, 'utf8')
        .split(/\r?\n/)
        .map((line) => line.replace(/\s+#.*$/, '').trim())
        .filter((line) => line && !line.startsWith('#')),
    );
  } catch (error) {
    throw new Error(`Baseline npm ausente ou ilegivel em ${path}: ${error.message}`);
  }
}

function advisoryId(via) {
  const ghsa = via.url?.match(/GHSA-[a-z0-9-]+/i)?.[0];
  if (ghsa) {
    return ghsa.toUpperCase();
  }
  if (via.url) {
    return via.url;
  }
  if (via.source) {
    return `npm-advisory:${via.source}`;
  }
  return null;
}

const report = loadJson(reportPath);
const baseline = loadBaseline(baselinePath);
const current = new Set();

for (const vulnerability of Object.values(report.vulnerabilities ?? {})) {
  for (const via of vulnerability.via ?? []) {
    if (typeof via === 'string') {
      continue;
    }

    const severity = via.severity ?? vulnerability.severity;
    if (severityRank[severity] < severityRank[auditLevel]) {
      continue;
    }

    const id = advisoryId(via);
    if (id) {
      current.add(id);
    }
  }
}

if (current.size === 0) {
  const count = Object.values(report.metadata?.vulnerabilities ?? {})
    .filter((_, index) => index >= severityRank[auditLevel])
    .reduce((total, value) => total + value, 0);
  if (count > 0) {
    throw new Error('npm audit reportou vulnerabilidades bloqueantes sem IDs comparaveis.');
  }
}

const added = [...current].filter((id) => !baseline.has(id)).sort();
const stale = [...baseline].filter((id) => !current.has(id)).sort();

console.log(
  `npm audit: ${current.size} advisory(s) ${auditLevel}+; ` +
    `${baseline.size} na baseline; ${added.length} novo(s).`,
);

if (stale.length > 0) {
  console.log('Advisories da baseline que nao aparecem mais (remover em PR dedicado):');
  stale.forEach((id) => console.log(`  - ${id}`));
}

if (added.length > 0) {
  console.error('Novos advisories bloqueantes:');
  added.forEach((id) => console.error(`  - ${id}`));
  process.exit(1);
}

