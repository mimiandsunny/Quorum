# Design System — stockinvest

Generated: 2026-05-01 by /design-consultation
Branch: `recommendation`
Source: extracted from `templates/dashboard.html` (Wave 1 + 1.5 implicit system) + Wave 2 plan rev 4 design decisions (DR1-DR9)

## Product Context

- **What this is:** AI Trading Research Tool — multi-agent debate system (bull vs bear analysts → trader synthesis → risk approval) producing actionable equity signals with precise entry/exit/stop levels, before 9:30 AM ET market open. Wave 2 adds an options expression layer (D1-D4 per CEO plan v1.4 rev 4).
- **Who it's for:** Solo trader/researcher, dogfooding own trading research with multi-agent reasoning + paper-trading validation.
- **Space/industry:** Quantitative trading research tools (peers in spirit: Bloomberg Terminal, ToS, Tastytrade, but a single-user tool, not a platform).
- **Project type:** Internal data-dense web app (App UI), localhost-only (`127.0.0.1:8000` per CEO plan decision 15), dark-mode only.

## Memorable Thing

"Hedge fund investment committee running on your laptop." Watching the agent debate unfold is the emotional signal — the thing that makes the tool worth opening every morning. Every design decision should serve that: serious computing tool, calm surface, dense readable data, no marketing-app chrome.

## Aesthetic Direction

- **Direction:** Industrial / Utilitarian — function-first, data-dense, calm surface hierarchy. Trader's terminal lineage, not SaaS dashboard chrome.
- **Decoration level:** **Minimal** — semantic color tokens carry hierarchy; no decorative borders, no shadows, no blobs, no gradients.
- **Mood:** A serious tool that respects the user's time. Glanceable in 3 seconds at 6:45 AM, scannable for 30 minutes during the post-coffee deep-dive.
- **Anti-patterns explicitly rejected:** SaaS card grids, purple/violet accents, decorative icons-in-circles, centered-everything, bubble-radius on every element, marketing-style hero copy, gradient buttons.

## Typography

Single-family system: **IBM Plex Sans** (body, UI, headings) + **IBM Plex Mono** (data, prices, contracts, code). Open-source under SIL Open Font License. Loaded from Bunny Fonts (privacy-respecting, no Google tracking) or self-hosted from `/static/fonts/`.

- **Display / hero / section headings:** IBM Plex Sans 600 (SemiBold)
- **Body / UI / table cells:** IBM Plex Sans 400 (Regular)
- **Strong emphasis (ticker, key numbers in chips):** IBM Plex Sans 700 (Bold)
- **Data / prices / contract symbols / code:** IBM Plex Mono 400 (Regular) with `font-variant-numeric: tabular-nums`
- **Loading strategy:** `<link rel="preconnect" href="https://fonts.bunny.net">` + `<link rel="stylesheet" href="https://fonts.bunny.net/css?family=ibm-plex-sans:400,600,700|ibm-plex-mono:400&display=swap">` in `<head>` of `templates/dashboard.html`. Adds ~120KB / ~50-150ms cold-load cost.
- **Fallback stack:** `'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif` (sans) and `'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace` (mono). System fonts kick in if Bunny Fonts is offline.

### Type scale

| Role | Size | Weight | Usage |
|------|------|--------|-------|
| h1 | 1.5rem (24px) | 600 | Page title ("AI Trading Signals") |
| h2 (section-head) | 1rem (16px) | 600 | Section titles ("Options Cockpit", "V2 Recommendation Ledger") |
| h3 | 0.85rem (13.6px) | 600 | Sub-section titles (debate-panel bull/bear) |
| body | 0.85rem (13.6px) | 400 | Table cells, body copy |
| small | 0.75rem (12px) | 400 | meta, action-subtext |
| micro | 0.7rem (11.2px) | 600 | column headers (uppercase, letter-spacing 0.05em), chips |
| nano | 0.64rem (10.2px) | 600 | option-tag, alpha-chip |

App UI exception: body type runs below 16px because data density is the product. Table cells must remain scannable in dense rows; >=14px would push the v2-grid card layout into a redesign.

## Color

- **Approach:** Restrained — semantic tokens only, accent (`--accent`) reserved for interactive elements, semantic colors (`--green`, `--red`, `--yellow`) reserved for status. No decorative color use.
- **Mode:** **Dark only.** No light-mode toggle planned. Localhost solo, mostly used pre-dawn through morning; dark theme is the only mode.

### Surfaces

```
--bg               #0a0a0f   /* page background */
--bg-surface       #0d0d18   /* panel default */
--bg-surface-hover #0d0d1a   /* table row hover */
--bg-elevated      #111122   /* elevated panel (debate-panel) */
--bg-chip          #1a1a2e   /* chip background, also --border value */
```

### Borders

```
--border         #1a1a2e     /* default panel + table border-bottom */
--border-row     #141420     /* table row separator (lighter than --border) */
--border-subtle  #222        /* filter chip border */
```

### Text (all verified ≥ 4.5:1 contrast on `--bg`)

```
--text-strong  #fff       /* headings, ticker, primary numbers */
--text         #e0e0e0    /* body */
--text-price   #cfcfd4    /* numeric values in data tables */
--text-muted   #9a9aa0    /* labels, captions (6.4:1) */
--text-dim     #8a8a90    /* column headers, metadata (5.5:1) */
--text-faint   #7a7a80    /* placeholder dashes, low-priority info (4.7:1) */
```

### Accent (info / interactive)

```
--accent              #7eb8ff
--accent-bg           #1e3a5f
--accent-border       #2d5a8e
--accent-hover-bg     #254d7a
--accent-hover-border #3d7ac0
```

### Semantic status (each with -bg / -border variants)

```
--green  #34d399  /* success, BUY, approved, cheap_vol, bullish */
--red    #f87171  /* error, SELL, rejected, bearish */
--yellow #fbbf24  /* warning, HOLD, partial-success, rich_vol */
--purple #a78bfa  /* reserved for special states (currently unused) */
```

### Spinner

```
--spinner-track  #333    /* spinner track ring */
```

## Spacing

- **Base unit:** 4px
- **Density:** Compact (App UI, data-dense)
- **Scale:**

```
2xs  2px    /* tight gaps between chip + value */
xs   4px    /* meta gaps, action-subtext margin */
sm   8px    /* default gap between siblings */
md   12px   /* table cell padding, panel inner padding */
lg   16px   /* section bottom margin, debate-panel padding */
xl   24px   /* body padding, section header bottom margin */
2xl  32px   /* not currently used, reserved */
3xl  48px   /* hero spacing if ever needed */
```

## Layout

- **Approach:** **Grid-disciplined**, App UI conventions throughout. Single full-width body with 24px padding, no max-width constraint (fills browser).
- **Grid systems in use:**
  - `.v2-grid` — `grid-template-columns: repeat(auto-fit, minmax(360px, 1fr))` for Recommendation cards
  - `.ticket-grid` — `grid-template-columns: repeat(6, minmax(120px, 1fr))` for ticket exposures
  - `.v2-metrics` — `grid-template-columns: repeat(4, 1fr)` for inner metric strips
  - `.strategy-summary` — `grid-template-columns: repeat(3, 1fr)` for strategy comparison
  - `.debate-round` — `grid-template-columns: 1fr 1fr` for two-column dialectical layout
- **Border radius scale:**

```
sm    4px    /* chips, decision badges */
md    6px    /* buttons (filter, run, options) */
lg    8px    /* panels, debate-panel, btn-run */
xl    10px   /* alpha-chip pills */
full  9999px /* none currently — reserved */
```

- **Responsive breakpoint:** Single `@media (max-width: 800px)` rule collapses `.strategy-summary` and `.ticket-grid` to fewer columns. Wave 2 adds: D4 two-column thesis panel collapses to single-column (DR7).

## Motion

- **Approach:** **Minimal-functional** — only transitions that aid comprehension; no entrance animations, no scroll-driven choreography.
- **Easing:** `ease` for color/background transitions (200ms `transition: all 0.2s` on buttons).
- **Spinner:** `animation: spin 0.8s linear infinite` (keyframe defined at line 113 of dashboard.html).
- **Duration tiers:**

```
micro    50-100ms    /* hover state changes (color, border) */
short    150-250ms   /* button state transitions, filter active */
medium   250-400ms   /* not currently used */
long     400-700ms   /* not currently used */
```

## Iconography

- **Style:** Unicode symbols and arrows over icon fonts. Avoids icon-font dependency, keeps the page lean.
- **Examples in use:** `↗` cross-link arrow, `▾` disclosure marker, `✕` close/error indicator, `▲ / △` Greeks-confidence (Wave 2 DR7 — TINY size, must have `aria-label` and `title` for screen reader announcement).
- **Rule:** No emoji in UI chrome. No icon-in-colored-circle decorations. Only meaningful glyphs.

## Component Vocabulary

The CSS classes that make up the system. Each one earns its existence (App UI rule: cards only when card IS the interaction).

### Chips

| Class | Purpose | Tokens used |
|-------|---------|-------------|
| `.recommendation-chip.trade/.blocked/.hold` | Recommendation status | semantic green/red/yellow |
| `.decision.BUY/.SELL/.HOLD` | Decision badge | semantic green/red/yellow |
| `.alpha-chip.long/.short/.flat` | Alpha exposure direction | semantic green/red/yellow |
| `.option-type.call/.put` | Option type marker | semantic green/red |
| `.iv-label.cheap/.rich/.fair/.unknown/.insufficient` | Implied volatility label (Wave 2 DR7 adds `.insufficient` = `--text-dim` on `--bg-chip`) | semantic + --text-dim |
| `.option-tag.unusual_flow/.cheap_vol/.rich_vol` | Options analytics tags | accent / green / yellow |

### Containers

| Class | Purpose |
|-------|---------|
| `.debate-panel` | Bull/bear debate, also reused for D4 thesis panel (Wave 2 DR5) |
| `.v2-grid` card | Recommendation row card (Wave 2 DR9 adds Protect field) |
| `.signals-table` | Dense data table with hover rows + tabular-nums |
| `.run-status` | Status bar with optional spinner |
| `.run-strip` | Aggregate run health strip with ok/warn/failed-list slots |
| `.options-status-row` (Wave 2 DR2) | 3-slot freshness/job/source row |

### Buttons

| Class | Purpose |
|-------|---------|
| `.btn-run` | Primary action button with `.running` / `.stop` / `.success` / `.error` state variants |
| `.btn-options` | Secondary accent button (refresh, etc.) |
| `.filters a` | Filter chip nav, with `.active` state |

### Disclosures

`<details><summary>` natively keyboard-accessible. Used for expandable Recommendation rows; Wave 2 DR9 introduces a two-tab pattern (`Spread` / `Protect`) inside the same disclosure.

## Wave 2 New Classes (committed, not yet implemented)

Per plan rev 4 DR1-DR9. All reuse existing tokens; no new colors, fonts, or radii are introduced.

| Class | Purpose | Token reuse |
|-------|---------|-------------|
| `.options-status-row` | DR2 3-slot status row under cockpit header | `.divider`, semantic + text-muted/yellow/red |
| `.option-thesis-panel` | DR5 two-column inline thesis container | `.debate-panel` background |
| `.option-thesis-leg` | DR5 one row per leg in spread | `.option-type` chip |
| `.option-payoff-svg` | DR5 80×40 inline payoff SVG container | --accent stroke, --green/--red fill |
| `.option-iv-history-section` | DR8 cold-start subsection wrapper | `.divider`, `.legacy-note` |
| `.iv-label.insufficient` | DR7 new IV label state for A5 cold-start | --text-dim on --bg-chip |
| `.thesis-feedback` | DR5 thumbs-up/down chip pair | `.recommendation-chip` semantic colors |
| `.greeks-confidence` | DR7 TINY ▲/△ icons, requires aria-label | semantic green / yellow |
| `.option-source-chip` | DR2 per-snapshot source badge | `.alpha-chip` shape |
| `.protect-field` | DR9 protect field on `.v2-grid` card | tabular-nums, `.option-type.put` chip |

## Accessibility

- **Color contrast:** All `--text-*` tokens verified ≥ 4.5:1 on `--bg` (5.5:1 to 6.4:1 for muted/dim/faint). Maintain when picking new tokens.
- **Color is never the only signal:** Every `--green / --red / --yellow` use must be paired with text or an icon (BUY/SELL/HOLD text on the chip, ✕ glyph on risk-reasons, etc.).
- **Focus visible:** Use `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }` on all interactive elements. Existing pattern on `.filters a:focus-visible`, `.btn-run:focus-visible`.
- **Keyboard nav:** Use `<details><summary>` over custom JS toggles where possible (native keyboard support, screen-reader announce).
- **Icon labels:** Any icon used as the only signal must carry `aria-label` and `title` attributes. Wave 2 DR7 explicitly applies this to ▲/△ Greeks-confidence icons.
- **Touch targets:** Localhost solo desktop = mouse target (24px+ acceptable). LAN exposure (TD-7) would require 44px review.

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-13 | Multi-agent trading research tool, dark-only App UI | Office-hours design doc — solo trader, localhost, pre-market routine |
| Wave 1 (2026-04-24) | Initial dashboard.html with semantic tokens, signals-table, decision/recommendation chips | Implicit system, validated by daily use |
| Wave 1.5 (2026-04-26) | v2-grid Recommendation cards, debate-panel for bull/bear, calibrated text contrast tokens | Three-portfolio engine; text colors recalibrated to ≥ 4.5:1 |
| 2026-04-29 | Wave 2 cockpit additions in plan v1.4 (D1-D4) | CEO plan; options as expression layer for equity signals |
| 2026-05-01 | Plan rev 2 → rev 3: 11 eng-review decisions (A1-A7, C1-C2, T1, P1, P2) | /plan-eng-review locked architecture |
| 2026-05-01 | Plan rev 3 → rev 4: 9 design decisions (DR1-DR9), ASCII wireframe, state coverage defaults rule | /plan-design-review locked panels |
| 2026-05-01 | DESIGN.md created from extracted dashboard.html system + Wave 2 additions | /design-consultation formalizes the implicit system |
| 2026-05-01 | Typography: IBM Plex Sans + IBM Plex Mono (replaces system-ui primary stack) | Closes TD-14; signals serious computing without Inter/Geist convergence |
