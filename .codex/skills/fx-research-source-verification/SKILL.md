---
name: fx-research-source-verification
description: Verify FX, statistical, API, broker, and Codex engineering claims against current primary sources and maintain the repository source ledger. Use before design decisions, citations, vendor assumptions, or periodic source refreshes.
---

# FX research source verification

## Purpose and inputs

Keep implementation claims traceable to real, current sources. Inputs are the claim/design question, affected module, jurisdiction/vendor, and review date.

## Procedure

1. Search primary sources first: official specifications, regulators, central banks, BIS/GFXC/CFTC, API/broker docs, and original papers. Technical questions use official docs/research papers.
2. Verify title, authors/owner, stable URL/DOI, publication/update date, exact supported claim, assumptions, and current availability. Never cite a search-results page.
3. Distinguish evidence from inference. Cross-check high-impact or unstable claims with another authoritative source when possible.
4. Translate only what the source supports into system behavior. Record limitations such as OTC fragmentation, revisions, publication lags, licensing, venue differences, and lack of proprietary flow.
5. Update `docs/research/SOURCE_LEDGER.md` with claim, source, URL, primary/secondary, implementation effect, limitation, checked date, and refresh need.
6. Re-check unstable API, schedule, regulation, model/product, and vendor claims before use. Preserve the previous design decision if new evidence is insufficient.

## Commands

```bash
rg -n "https?://|DOI|source|出典" docs research_pack fx_backtester fx_intel
```

For Codex/OpenAI behavior, use official OpenAI documentation only. For statistics, prefer the original PBO/DSR/calibration/Reality Check/SPA papers. For FX structure use BIS and the FX Global Code; for COT/FRED use official API/release docs.

## Pass and fail conditions

Pass requires a resolvable authoritative source directly supporting the claim, an implementation mapping, explicit limitations, and a checked date. Broken/unverifiable URLs, secondary-only financial claims, promotional vendor claims without contract detail, or invented precision fail verification.

## Output format

For each claim: verdict (`verified`, `partially supported`, `unsupported`, `stale`); source/link/type/date; supported scope; inference; design effect; limitation; next review date. Update the ledger in the same change.

## Prohibited actions

Do not invent papers, URLs, quotes, API guarantees, or statistics; do not treat blogs/X/YouTube as financial evidence; do not exceed source quotation limits; do not describe public volume/candles as dealer order flow.

## Example

“Use `$fx-research-source-verification` to verify COT publication availability, FRED vintage semantics, and the PBO/DSR formulas before updating the validation protocol.”
