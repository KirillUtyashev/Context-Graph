# Evaluation Report: Context-Graph Retrieval Benchmark

**Generated:** 2026-03-30 17:13 UTC  
**Questions:** 90 (80 main\_v2 + 10 bug\_pr)  
**Conditions:** A (Baseline) · C (Context-Graph MCP) · D (Codebase-Memory MCP)  

## Executive Summary

| Metric | A — Baseline | C — Context-Graph MCP | D — Codebase-Memory MCP |
|--------|------|------|------|
| **Hit Rate** | 98.9% (89/90) | 92.2% (83/90) | 92.2% (83/90) |
| **Avg Tokens / Question** | 54,271 | 116,208 | 100,164 |
| **Total Tokens (90 Qs)** | 4,884,373 | 10,342,481 | 9,014,770 |
| **Token Efficiency** _(hits / 1k tokens)_ | 0.0182 | 0.0079 | 0.0092 |
| **Token Δ vs Baseline** | — | +114.1% | +84.6% |

> **Token Efficiency** = Hit Rate / (Avg Tokens / 1000). Higher is better.

## Accuracy Analysis

### Accuracy by Question Type

| Category | A hits/total | A% | C hits/total | C% | D hits/total | D% |
|----------|-------------|----|----|----|----|-----|
| debugging and provenance | 25/25 | 100.0% | 24/25 | 96.0% | 24/25 | 96.0% |
| information extraction | 8/8 | 100.0% | 8/8 | 100.0% | 8/8 | 100.0% |
| knowledge update | 16/16 | 100.0% | 15/16 | 93.8% | 15/16 | 93.8% |
| multi hop retrieval | 28/29 | 96.6% | 26/29 | 89.7% | 24/29 | 82.8% |
| temporal reasoning | 12/12 | 100.0% | 10/12 | 83.3% | 12/12 | 100.0% |

### Accuracy by Dataset

| Category | A hits/total | A% | C hits/total | C% | D hits/total | D% |
|----------|-------------|----|----|----|----|-----|
| bug pr | 10/10 | 100.0% | 9/10 | 90.0% | 10/10 | 100.0% |
| main v2 | 79/80 | 98.8% | 74/80 | 92.5% | 73/80 | 91.2% |

### Accuracy by Hop Count

| Category | A hits/total | A% | C hits/total | C% | D hits/total | D% |
|----------|-------------|----|----|----|----|-----|
| 1 | 8/8 | 100.0% | 8/8 | 100.0% | 8/8 | 100.0% |
| 2 | 13/13 | 100.0% | 12/13 | 92.3% | 13/13 | 100.0% |
| 3 | 21/21 | 100.0% | 19/21 | 90.5% | 20/21 | 95.2% |
| 4 | 6/6 | 100.0% | 6/6 | 100.0% | 5/6 | 83.3% |
| 5 | 14/15 | 93.3% | 13/15 | 86.7% | 13/15 | 86.7% |
| 6 | 27/27 | 100.0% | 25/27 | 92.6% | 24/27 | 88.9% |

## Token Consumption

### Overall Token Consumption

| Condition | Avg Tokens | Total Tokens | Min | Max |
|-----------|-----------|-------------|-----|-----|
| A — Baseline | 54,271 | 4,884,373 | 22,839 | 414,731 |
| C — Context-Graph MCP | 116,208 | 10,342,481 | 21,939 | 565,083 |
| D — Codebase-Memory MCP | 100,164 | 9,014,770 | 54,127 | 257,935 |

### Token Consumption by Question Type

| Category | Avg A | Avg C | Avg D | C vs A | D vs A |
|----------|-------|-------|-------|--------|--------|
| debugging and provenance | 71,264 | 122,681 | 109,428 | +72.2% | +53.6% |
| information extraction | 28,960 | 47,675 | 93,544 | +64.6% | +223.0% |
| knowledge update | 54,355 | 153,644 | 101,793 | +182.7% | +87.3% |
| multi hop retrieval | 53,563 | 118,918 | 101,041 | +122.0% | +88.6% |
| temporal reasoning | 37,341 | 92,483 | 80,988 | +147.7% | +116.9% |

### Token Consumption by Dataset

| Category | Avg A | Avg C | Avg D | C vs A | D vs A |
|----------|-------|-------|-------|--------|--------|
| bug pr | 99,330 | 116,382 | 109,394 | +17.2% | +10.1% |
| main v2 | 48,638 | 116,188 | 99,010 | +138.9% | +103.6% |

### Token Consumption by Hop Count

| Category | Avg A | Avg C | Avg D | C vs A | D vs A |
|----------|-------|-------|-------|--------|--------|
| 1 | 28,960 | 47,675 | 93,544 | +64.6% | +223.0% |
| 2 | 47,826 | 61,471 | 83,562 | +28.5% | +74.7% |
| 3 | 37,882 | 102,538 | 100,820 | +170.7% | +166.1% |
| 4 | 38,272 | 126,718 | 113,252 | +231.1% | +195.9% |
| 5 | 94,432 | 146,119 | 95,660 | +54.7% | +1.3% |
| 6 | 58,863 | 155,655 | 109,203 | +164.4% | +85.5% |

## Token Efficiency

> **Definition:** Efficiency = Hit Rate (%) ÷ (Avg Tokens / 1,000)  
> Interpretable as *hits per 1,000 tokens*. Higher = better cost-effectiveness.

### Overall Efficiency

| Condition | Hit Rate | Avg Tokens | Efficiency |
|-----------|---------|-----------|-----------|
| A — Baseline | 98.9% | 54,271 | 0.0182 |
| C — Context-Graph MCP | 92.2% | 116,208 | 0.0079 |
| D — Codebase-Memory MCP | 92.2% | 100,164 | 0.0092 |

### Efficiency by Question Type

| Type | A Eff | C Eff | D Eff |
|------|-------|-------|-------|
| information extraction | 0.0345 | 0.0210 | 0.0107 |
| multi hop retrieval | 0.0180 | 0.0075 | 0.0082 |
| knowledge update | 0.0184 | 0.0061 | 0.0092 |
| temporal reasoning | 0.0268 | 0.0090 | 0.0123 |
| debugging and provenance | 0.0140 | 0.0078 | 0.0088 |

## Miss Analysis

### Miss Counts and Classification

| Condition | Total MISSes | guessed\_wrong | abstained | unclassified |
|-----------|-------------|----------------|-----------|--------------|
| A — Baseline | 1 | 0 | 0 | 1 |
| C — Context-Graph MCP | 7 | 0 | 0 | 7 |
| D — Codebase-Memory MCP | 7 | 7 | 0 | 0 |

### Missed Questions per Condition

**A — Baseline** (1 MISSes):
- `main_v2_Q111` (multi hop retrieval, 5 hops) — unclassified

**C — Context-Graph MCP** (7 MISSes):
- `bug_pr_Q5` (debugging and provenance, 5 hops) — unclassified
- `main_v2_Q47` (multi hop retrieval, 3 hops) — unclassified
- `main_v2_Q83` (temporal reasoning, 3 hops) — unclassified
- `main_v2_Q85` (temporal reasoning, 2 hops) — unclassified
- `main_v2_Q109` (multi hop retrieval, 6 hops) — unclassified
- `main_v2_Q110` (multi hop retrieval, 6 hops) — unclassified
- `main_v2_Q125` (knowledge update, 5 hops) — unclassified

**D — Codebase-Memory MCP** (7 MISSes):
- `main_v2_Q50` (multi hop retrieval, 4 hops) — guessed_wrong
- `main_v2_Q97` (debugging and provenance, 3 hops) — guessed_wrong
- `main_v2_Q106` (multi hop retrieval, 5 hops) — guessed_wrong
- `main_v2_Q109` (multi hop retrieval, 6 hops) — guessed_wrong
- `main_v2_Q110` (multi hop retrieval, 6 hops) — guessed_wrong
- `main_v2_Q120` (multi hop retrieval, 6 hops) — guessed_wrong
- `main_v2_Q125` (knowledge update, 5 hops) — guessed_wrong

### Miss Overlap

**Missed by all 3 conditions:** none

**Missed by C or D but not A (MCP-only misses):** bug_pr_Q5, main_v2_Q106, main_v2_Q109, main_v2_Q110, main_v2_Q120, main_v2_Q125, main_v2_Q47, main_v2_Q50, main_v2_Q83, main_v2_Q85, main_v2_Q97

**Missed by C but not D:** bug_pr_Q5, main_v2_Q47, main_v2_Q83, main_v2_Q85

**Missed by D but not C:** main_v2_Q106, main_v2_Q120, main_v2_Q50, main_v2_Q97

## Appendix: Per-Question Results

All 90 questions with verdicts and token counts for each condition.

| ID | Type | Hops | A verdict | A tokens | C verdict | C tokens | D verdict | D tokens |
|----|------|------|-----------|----------|-----------|----------|-----------|----------|
| bug_pr_Q5 | debugging and provenan | 5 | HIT | 81,433 | **MISS** | 111,119 | HIT | 121,099 |
| main_v2_Q47 | multi hop retrieval | 3 | HIT | 31,211 | **MISS** | 101,190 | HIT | 97,483 |
| main_v2_Q50 | multi hop retrieval | 4 | HIT | 29,495 | HIT | 38,851 | **MISS** | 90,709 |
| main_v2_Q83 | temporal reasoning | 3 | HIT | 32,260 | **MISS** | 252,720 | HIT | 84,780 |
| main_v2_Q85 | temporal reasoning | 2 | HIT | 36,117 | **MISS** | 101,498 | HIT | 68,298 |
| main_v2_Q97 | debugging and provenan | 3 | HIT | 41,936 | HIT | 182,117 | **MISS** | 113,191 |
| main_v2_Q106 | multi hop retrieval | 5 | HIT | 59,646 | HIT | 38,772 | **MISS** | 86,252 |
| main_v2_Q109 | multi hop retrieval | 6 | HIT | 77,696 | **MISS** | 159,077 | **MISS** | 83,309 |
| main_v2_Q110 | multi hop retrieval | 6 | HIT | 68,558 | **MISS** | 65,366 | **MISS** | 57,614 |
| main_v2_Q111 | multi hop retrieval | 5 | **MISS** | 35,501 | HIT | 157,512 | HIT | 67,783 |
| main_v2_Q120 | multi hop retrieval | 6 | HIT | 30,165 | HIT | 119,427 | **MISS** | 146,309 |
| main_v2_Q125 | knowledge update | 5 | HIT | 166,550 | **MISS** | 277,371 | **MISS** | 94,451 |
| | | | | | | | | |
| bug_pr_Q1 | debugging and provenan | 6 | HIT | 64,453 | HIT | 100,142 | HIT | 69,958 |
| bug_pr_Q2 | debugging and provenan | 6 | HIT | 52,633 | HIT | 41,215 | HIT | 179,826 |
| bug_pr_Q3 | debugging and provenan | 6 | HIT | 138,025 | HIT | 281,604 | HIT | 79,341 |
| bug_pr_Q4 | debugging and provenan | 6 | HIT | 69,738 | HIT | 140,557 | HIT | 257,935 |
| bug_pr_Q6 | debugging and provenan | 6 | HIT | 58,241 | HIT | 23,950 | HIT | 58,786 |
| bug_pr_Q7 | debugging and provenan | 6 | HIT | 30,180 | HIT | 186,462 | HIT | 114,508 |
| bug_pr_Q8 | debugging and provenan | 6 | HIT | 33,671 | HIT | 85,842 | HIT | 56,427 |
| bug_pr_Q9 | debugging and provenan | 6 | HIT | 50,191 | HIT | 76,549 | HIT | 87,685 |
| bug_pr_Q10 | debugging and provenan | 5 | HIT | 414,731 | HIT | — | HIT | 68,376 |
| main_v2_Q15 | information extraction | 1 | HIT | 30,686 | HIT | 39,005 | HIT | 93,855 |
| main_v2_Q16 | information extraction | 1 | HIT | 29,179 | HIT | 49,850 | HIT | 151,172 |
| main_v2_Q17 | information extraction | 1 | HIT | 28,065 | HIT | 48,522 | HIT | 84,202 |
| main_v2_Q18 | information extraction | 1 | HIT | 29,259 | HIT | 49,483 | HIT | 94,586 |
| main_v2_Q19 | information extraction | 1 | HIT | 24,500 | HIT | 23,155 | HIT | 55,584 |
| main_v2_Q21 | information extraction | 1 | HIT | 28,349 | HIT | 21,939 | HIT | 65,007 |
| main_v2_Q23 | information extraction | 1 | HIT | 30,684 | HIT | 64,335 | HIT | 81,189 |
| main_v2_Q25 | information extraction | 1 | HIT | 30,959 | HIT | 85,110 | HIT | 122,757 |
| main_v2_Q30 | multi hop retrieval | 3 | HIT | 29,612 | HIT | 38,058 | HIT | 82,406 |
| main_v2_Q31 | multi hop retrieval | 3 | HIT | 22,839 | HIT | 23,596 | HIT | 59,289 |
| main_v2_Q33 | multi hop retrieval | 3 | HIT | 31,453 | HIT | 35,190 | HIT | 145,486 |
| main_v2_Q34 | multi hop retrieval | 3 | HIT | 51,909 | HIT | 86,990 | HIT | 82,077 |
| main_v2_Q38 | multi hop retrieval | 3 | HIT | 39,935 | HIT | 125,943 | HIT | 57,420 |
| main_v2_Q42 | multi hop retrieval | 2 | HIT | 33,588 | HIT | 81,986 | HIT | 59,719 |
| main_v2_Q43 | multi hop retrieval | 3 | HIT | 41,603 | HIT | 111,673 | HIT | 139,767 |
| main_v2_Q48 | multi hop retrieval | 3 | HIT | 50,438 | HIT | 128,083 | HIT | 88,499 |
| main_v2_Q49 | multi hop retrieval | 3 | HIT | 30,801 | HIT | 37,746 | HIT | 58,933 |
| main_v2_Q54 | knowledge update | 2 | HIT | 27,706 | HIT | 135,863 | HIT | 110,567 |
| main_v2_Q55 | knowledge update | 2 | HIT | 31,054 | HIT | 38,083 | HIT | 74,088 |
| main_v2_Q57 | knowledge update | 3 | HIT | 30,179 | HIT | 124,335 | HIT | 93,440 |
| main_v2_Q58 | knowledge update | 2 | HIT | 50,339 | HIT | 51,163 | HIT | 86,512 |
| main_v2_Q61 | knowledge update | 2 | HIT | 31,355 | HIT | 56,553 | HIT | 117,206 |
| main_v2_Q63 | knowledge update | 3 | HIT | 31,099 | HIT | 114,096 | HIT | 150,997 |
| main_v2_Q66 | knowledge update | 2 | HIT | 165,051 | HIT | 63,404 | HIT | 115,473 |
| main_v2_Q67 | knowledge update | 2 | HIT | 41,784 | HIT | 45,016 | HIT | 54,127 |
| main_v2_Q68 | knowledge update | 2 | HIT | 34,817 | HIT | 37,852 | HIT | 117,939 |
| main_v2_Q70 | knowledge update | 3 | HIT | 32,243 | HIT | 89,718 | HIT | 69,158 |
| main_v2_Q76 | temporal reasoning | 3 | HIT | 35,716 | HIT | 73,253 | HIT | 113,906 |
| main_v2_Q77 | temporal reasoning | 2 | HIT | 32,865 | HIT | 40,682 | HIT | 84,435 |
| main_v2_Q79 | temporal reasoning | 2 | HIT | 30,123 | HIT | 23,977 | HIT | 69,009 |
| main_v2_Q80 | temporal reasoning | 2 | HIT | 66,055 | HIT | 70,244 | HIT | 73,293 |
| main_v2_Q86 | debugging and provenan | 3 | HIT | 105,731 | HIT | 285,909 | HIT | 163,961 |
| main_v2_Q89 | debugging and provenan | 3 | HIT | 35,355 | HIT | 46,366 | HIT | 89,301 |
| main_v2_Q90 | debugging and provenan | 3 | HIT | 30,461 | HIT | 53,487 | HIT | 59,100 |
| main_v2_Q91 | debugging and provenan | 3 | HIT | 31,585 | HIT | 66,041 | HIT | 88,794 |
| main_v2_Q93 | debugging and provenan | 4 | HIT | 40,436 | HIT | 64,590 | HIT | 96,583 |
| main_v2_Q94 | debugging and provenan | 3 | HIT | 28,571 | HIT | 74,721 | HIT | 107,312 |
| main_v2_Q95 | debugging and provenan | 2 | HIT | 40,886 | HIT | 52,801 | HIT | 55,641 |
| main_v2_Q98 | debugging and provenan | 3 | HIT | 30,595 | HIT | 102,072 | HIT | 171,915 |
| main_v2_Q101 | multi hop retrieval | 6 | HIT | 75,027 | HIT | 116,090 | HIT | 131,687 |
| main_v2_Q102 | multi hop retrieval | 6 | HIT | 42,068 | HIT | 25,808 | HIT | 58,166 |
| main_v2_Q103 | multi hop retrieval | 6 | HIT | 44,074 | HIT | 341,268 | HIT | 110,496 |
| main_v2_Q104 | multi hop retrieval | 6 | HIT | 29,913 | HIT | 104,262 | HIT | 57,107 |
| main_v2_Q107 | multi hop retrieval | 5 | HIT | 47,718 | HIT | 87,804 | HIT | 143,188 |
| main_v2_Q108 | multi hop retrieval | 5 | HIT | 84,120 | HIT | 93,535 | HIT | 101,615 |
| main_v2_Q112 | multi hop retrieval | 6 | HIT | 31,103 | HIT | 156,121 | HIT | 84,143 |
| main_v2_Q113 | multi hop retrieval | 6 | HIT | 44,240 | HIT | 108,685 | HIT | 140,858 |
| main_v2_Q114 | multi hop retrieval | 4 | HIT | 44,564 | HIT | 327,444 | HIT | 221,209 |
| main_v2_Q115 | multi hop retrieval | 5 | HIT | 32,245 | HIT | 179,212 | HIT | 109,188 |
| main_v2_Q116 | multi hop retrieval | 6 | HIT | 84,209 | HIT | 274,928 | HIT | 118,341 |
| main_v2_Q117 | multi hop retrieval | 5 | HIT | 93,199 | HIT | 101,104 | HIT | 127,813 |
| main_v2_Q118 | multi hop retrieval | 5 | HIT | 236,400 | HIT | 182,914 | HIT | 123,311 |
| main_v2_Q121 | knowledge update | 5 | HIT | 32,907 | HIT | 529,321 | HIT | 116,574 |
| main_v2_Q122 | knowledge update | 6 | HIT | 49,701 | HIT | 565,083 | HIT | 110,346 |
| main_v2_Q123 | knowledge update | 6 | HIT | 43,753 | HIT | 161,805 | HIT | 57,911 |
| main_v2_Q124 | knowledge update | 6 | HIT | 68,924 | HIT | 99,650 | HIT | 191,761 |
| main_v2_Q126 | knowledge update | 5 | HIT | 32,218 | HIT | 68,995 | HIT | 68,136 |
| main_v2_Q131 | temporal reasoning | 4 | HIT | 41,984 | HIT | 122,423 | HIT | 119,906 |
| main_v2_Q132 | temporal reasoning | 4 | HIT | 31,017 | HIT | 23,173 | HIT | 79,477 |
| main_v2_Q133 | temporal reasoning | 4 | HIT | 42,139 | HIT | 183,826 | HIT | 71,629 |
| main_v2_Q134 | temporal reasoning | 5 | HIT | 29,660 | HIT | 78,645 | HIT | 69,428 |
| main_v2_Q135 | temporal reasoning | 5 | HIT | 29,837 | HIT | 41,666 | HIT | 70,640 |
| main_v2_Q138 | temporal reasoning | 5 | HIT | 40,314 | HIT | 97,694 | HIT | 67,050 |
| main_v2_Q139 | debugging and provenan | 6 | HIT | 125,368 | HIT | 485,398 | HIT | 170,528 |
| main_v2_Q140 | debugging and provenan | 6 | HIT | 76,707 | HIT | 164,806 | HIT | 159,630 |
| main_v2_Q141 | debugging and provenan | 6 | HIT | 44,819 | HIT | 129,430 | HIT | 81,913 |
| main_v2_Q142 | debugging and provenan | 6 | HIT | 43,956 | HIT | 58,309 | HIT | 58,799 |
| main_v2_Q143 | debugging and provenan | 6 | HIT | 70,574 | HIT | 74,250 | HIT | 110,997 |
| main_v2_Q144 | debugging and provenan | 6 | HIT | 41,319 | HIT | 56,601 | HIT | 114,098 |
