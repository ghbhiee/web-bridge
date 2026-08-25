/* @web-bridge-capability
{
  "id": "extract-tables",
  "title": "提取表格",
  "description": "把页面里所有 <table> 提取为结构化数据（表头 + 行）。适合抓价格表、行情、报表、对比表。可选返回 CSV。",
  "kind": "extract",
  "match": ["*"],
  "params": {
    "csv": {"type": "boolean", "default": false, "description": "同时返回 CSV 文本"},
    "min_rows": {"type": "number", "default": 1, "description": "少于这么多行的表格忽略（滤掉布局用表格）"},
    "index": {"type": "number", "min": 0, "description": "只取第 N 个表格（从 0 开始），不填则全部"}
  }
}
*/
const minRows = args.min_rows ?? 1;
const cell = (td) => (td.innerText || td.textContent || "").trim().replace(/\s+/g, " ");

let tables = [...document.querySelectorAll("table")];
if (typeof args.index === "number") tables = tables[args.index] ? [tables[args.index]] : [];

const out = [];
for (const t of tables) {
  const rows = [...t.querySelectorAll("tr")];
  if (rows.length < minRows) continue;

  // header = first row that is all <th>, else first row
  let header = [];
  let bodyRows = rows;
  const firstCells = [...rows[0].children];
  if (firstCells.length && firstCells.every((c) => c.tagName === "TH")) {
    header = firstCells.map(cell);
    bodyRows = rows.slice(1);
  } else {
    const ths = [...t.querySelectorAll("thead th")];
    if (ths.length) {
      header = ths.map(cell);
      bodyRows = rows.filter((r) => !r.closest("thead"));
    }
  }

  const data = bodyRows
    .map((r) => [...r.children].map(cell))
    .filter((r) => r.some((v) => v !== ""));
  if (!data.length) continue;

  const caption = t.querySelector("caption");
  out.push({
    caption: caption ? cell(caption) : null,
    header,
    rows: data,
    row_count: data.length,
    col_count: Math.max(...data.map((r) => r.length)),
    // rows as objects when we have a usable header
    records: header.length
      ? data.map((r) => Object.fromEntries(header.map((h, i) => [h || `col${i}`, r[i] ?? ""])))
      : undefined,
  });
}

if (args.csv) {
  const esc = (v) => (/[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v);
  for (const t of out) {
    t.csv = [t.header.length ? t.header : null, ...t.rows]
      .filter(Boolean)
      .map((r) => r.map(esc).join(","))
      .join("\n");
  }
}

return { count: out.length, tables: out };
