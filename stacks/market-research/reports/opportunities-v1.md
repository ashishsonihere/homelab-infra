# Opportunities v1 — first full analysis run
_Run 2026-06-29. Tier-3 (`AN_TOP_N=20000`, DeepSeek-V3) → Tier-4 (`AN_TIER4_TOP_N=50`). Cost ≈ **$0.15** total._

## What ran
- **Tier-3:** 20,000 top `pain_signals` (ranked by `(score+1)×dup_count`) → DeepSeek grouped them into **523 problem_statements** (538 total incl. prior proof). Cost $0.142.
- **Tier-4:** top 50 statements → web-style validation + 3-axis scoring (**wedge 0.5 / wave 0.3 / edge 0.2**) + a saturation hard-gate → **51 opportunities**, **154 competitors** mapped. Cost ~$0.01.

## ⚠ Read this before trusting the scores
- **`final_score` is currently near-useless as a ranking.** The saturation hard-gate zeros any opportunity with **≥3 funded incumbents → final_score 0**. Result: **50 of 51 got 0.0; exactly 1 "passed" (3.5).**
- **The incumbent count is LLM-guessed, not web-verified** (Tier-4 does no real web/funding lookup yet — "Bug 2"). For any common pain the model "knows" ≥3 tools exist, so the gate fires on almost everything. **Don't over-index on the single gate-survivor.**
- **The ranking to trust is the pre-gate composite** (`wedge×.5 + wave×.3 + edge×.2`), with saturation as a *flag to investigate*, not a kill. That's what the table below uses.
- Scores cluster tightly (3.5–4.1) — LLM scoring doesn't differentiate sharply. And there's heavy **duplication** (ecom return/claim fraud appears ~5×). The duplication is itself a strong demand signal, but a dedup/cluster pass is needed.

## Top candidates (pre-gate composite, highest first)
| # | Score | Funded* | ICP | Wave | Problem |
|---|---|---|---|---|---|
| 1 | 4.10 | 3 | saas_op | AI-native ops | Run resource-intensive LLM tasks (doc/audio analysis) cost-effectively on **local hardware** |
| 2 | 4.10 | 3 | saas_op | vertical AI agents | **AI agents shipped with critical security holes** → breach/unauthorized-access exposure |
| 3 | 3.80 | 3 | **ecom** | AI-native ops | **Ecom sellers hit by fraudulent claims / "empty package" demands** → ops strain + account-suspension risk |
| 4 | 3.70 | 2 | saas_op | AI-native ops | Bootstrapped founders: ship MVPs fast vs. tech debt + resource limits |
| 5 | 3.70 | 2 | saas_op | AI-native ops | Founders waste months building for **non-existent / unvalidated problems** |
| 6 | 3.70 | 3 | saas_op | AI-native ops | SaaS **unsustainable costs from high API usage** + no monetization |
| 7 | 3.70 | 3 | saas_op | AI-native ops | **Review bombing** of small businesses (ex-employees/influencers), no recourse |
| 8 | 3.70 | 3 | **ecom** | vertical AI agents | Ecom sellers: unreasonable customer demands → suspension risk |
| 9 | 3.50 | 2 | **ecom** | AI-native ops | **Etsy: dropshippers faking "handmade"** undermine genuine creators _(only gate-survivor, final 3.5)_ |
| 10 | 3.50 | 3 | ecom | vertical AI agents | Ecom: unreasonable refund demands → suspension risk |
| 11 | 3.50 | 3 | saas_op | AI-native ops | Bootstrapped founders: MVP speed vs. tech debt (dup of #4) |
| 12 | 3.50 | 3 | saas_op | — | Entrepreneurs lose equity when personal/marital boundaries blur business assets |

\* LLM-guessed incumbent count — verify before trusting.

## The themes that recur (recurrence = signal)
1. **Ecom return/claim/refund fraud & unreasonable-customer-demand** — appears ~5× (#3, #8, #10, +others). Strongest *demand* signal in the set, and it sits squarely on the founder's **ecom/D2C operator edge**. (There is already a `Validation-Sprint-Return-Fraud.md` note in the vault — the data independently surfaced the same thing.)
2. **AI workload cost / local-hardware LLM ops** (#1, #6) — aligns with the data-sovereignty + India-cost-base + self-hosted edge. But likely genuinely crowded (Ollama, vLLM, etc.).
3. **AI-agent security** (#2) — hot, fundable, but enterprise-sales-heavy for a solo founder.
4. **Review-bombing recourse** (#7) — acute, emotional pain; less obviously crowded.
5. **Founder/MVP validation** (#4, #5) — meta (you'd be selling to people like you); recurs but easy to build a "vitamin."

## Founder-fit shortlist (my read)
- **#3 / ecom return & claim fraud** — best edge-fit + strongest recurrence. Worth a manual validation sprint first.
- **#7 / review-bombing recourse** — clean, acute, narrower competition.
- **#1 / local LLM cost ops** — best wave/sovereignty fit, but check saturation honestly.

## Next steps to make v2 trustworthy
1. **Dedup/cluster** the 51 (merge the ~5 ecom-returns variants into one) so the ranking isn't polluted by repeats.
2. **Fix Bug 2** — give Tier-4 real web/funding lookup (or cross-reference `funded_companies`/`vc_firms`) so the saturation gate fires on *real* competition, not LLM guesses. Then re-score → a `final_score` you can actually rank by.
3. **Manually validate the top 2-3** against the raw `pain_signals` (read the actual quotes) before committing.
