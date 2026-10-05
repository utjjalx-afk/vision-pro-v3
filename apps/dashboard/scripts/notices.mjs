import { readFile, writeFile } from "node:fs/promises";
const packages = ["lightweight-charts", "react", "react-dom"];
const sections = [
  "Third-party notices\n\nTradingView Lightweight Charts(TM)\nCopyright (c) 2025 TradingView, Inc. https://www.tradingview.com/",
];
for (const name of packages) {
  sections.push(
    `\n\n=== ${name} ===\n${await readFile(new URL(`../node_modules/${name}/LICENSE`, import.meta.url), "utf8")}`,
  );
}
await writeFile(
  new URL("../dist/assets/third-party-notices.txt", import.meta.url),
  sections.join(""),
  "utf8",
);
