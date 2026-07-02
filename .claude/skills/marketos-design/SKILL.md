---
name: marketos-design
description: Apply the MarketOS institutional design system to any frontend work in this codebase — colors, typography, spacing, components, the "number is the hero" identity, the signature experiences, and the Thesis Radar visualization. Use whenever creating, restyling, reviewing, or adding any UI, page, component, table, chart, form, or styling for MarketOS.
---

# MarketOS Design System

You are styling **MarketOS — an Institutional Quant Trading Operating System.** This skill is the single source of truth for how it looks and behaves. Derive every visual decision from the tokens below; never hardcode a color, size, or radius. (The full product narrative, commercial surfaces, and phased plan live in the implementation brief — this skill is the always-on "how to build it.")

## The one rule

**The number is the hero. Color is information.** Render the whole app in a calm, near-monochrome canvas so the only thing carrying chromatic energy is live market data — P&L, deltas, risk, status. Chrome is quiet; data is loud. Wow comes from precision, density, and speed — never from flashy motion or gradients (those break the institutional positioning).

## Frozen — never change

Backend, APIs, database, the engines (research / portfolio / risk / execution / recommendation), authentication, data flow, architecture. **UI/presentation only.** If a UI change appears to require a backend, API, schema, or auth change — **stop and flag it**, do not implement it.

## Tokens (single source of truth)

Implement as CSS custom properties; components consume the variables only.

**Color — dark (primary):**
`--canvas:#0A0E14` · `--surface:#111720` · `--surface-raised:#161D28` · `--border-hairline:#1E2630` · `--border-strong:#2A333F` · `--text-primary:#E7EBF2` · `--text-secondary:#9AA3B2` · `--text-tertiary:#646D7C` · `--accent:#4C8DFF` · `--accent-hover:#6BA0FF`

**Color — semantic (DATA ONLY, never decorative):**
`--pos:#2FB67A` (up/profit/long/pass) · `--neg:#E5484D` (down/loss/short/fail) · `--warn:#E8A33D` (alerts only) · `--crit:#E5484D` (critical only)

**Color — light (same names, remapped):**
`--canvas:#F7F8FA` · `--surface:#FFFFFF` · `--surface-raised:#FFFFFF` · `--border-hairline:#E6E9EE` · `--border-strong:#D4D9E0` · `--text-primary:#0B0F17` · `--text-secondary:#5B6472` · `--text-tertiary:#8B93A1` · `--accent:#2F6FE0` · `--pos:#138A5E` · `--neg:#D13B40`

**Typography:** UI face = existing project grotesque or **Inter**. Numeric/data face = **JetBrains Mono** / **IBM Plex Mono**; if staying on Inter, force `font-variant-numeric: tabular-nums` on every numeric element. Scale (px/line-height): `11/16` metadata · `12/16` caption · `13/18` table & dense body · `14/20` body · `16/22` widget title · `20/26` section · `24/30` page title (max). Weights: 400 body, 500 labels/widget titles, 600 section/page. **No oversized headings.**

**Spacing — 4px grid:** `2,4,6,8,12,16,20,24,32,40,48`. Default gutter `16`. Density is the default.

**Radius:** `4px` inputs/badges · `6px` panels/cards · `8px` modals/palette. Nothing rounder.

**Elevation:** prefer hairline border + one-step surface shift over shadows. One subtle shadow only for popovers / palette / modals.

**Motion:** `100–150ms`, easing `cubic-bezier(0.2,0,0,1)`. No bounce, spring, parallax, pulse, or spin. Respect `prefers-reduced-motion`.

## Theming

CSS variables at `:root` (dark) and `[data-theme="light"]`. Toggle persists via the **existing** settings store only — do not add new persistence infrastructure. Every component, chart, and the Thesis Radar must theme automatically by reading variables (e.g. `stroke: var(--accent)`).

## Numerics (the institutional hallmark — get this right)

- All numerals **tabular and monospaced**; numbers never change width on update.
- Right-align numeric table columns; align to the decimal.
- Centralized precision: prices to instrument tick size; percentages 2 dp; large notional grouped. Offer **Indian (lakh/crore)** and international (K/M/B) grouping — user-selectable, formatted in one utility, never ad-hoc.
- P&L color applied to the **value** (and an optional ▲▼ glyph), not a whole-row fill.
- **Colorblind-safe mode:** rely on +/− and ▲▼ glyphs in addition to color (never color alone).
- **Update flash:** a value change flashes a brief 120ms semantic tint, then settles. No constant flicker.
- Timestamps/IDs monospaced with explicit timezone (IST default, configurable); show "as of HH:MM:SS" freshness and disclose any data delay.

## Components

- **Tables:** dense rows (`32px` default, `28px` compact). Sticky headers. Right-aligned tabular numerics. Sortable, filterable, searchable; resizable/reorderable/hideable columns. Hover = subtle surface shift (not neon). Keyboard nav (↑↓ rows, enter opens). Inline sparklines/delta bars. **No zebra striping — hairline row separators.** Virtualize any table that can exceed ~100 rows.
- **Panels over cards:** prefer panels/grids/sections with hairline borders. A "card" is just a panel with a header — no shadow stacks, no oversized rounded boxes.
- **Badges:** one component, semantic tint at low opacity, `12px` text, `4px` radius. Set: `Research Ready · Monitoring · Open Position · High Conviction · Watch · Risk · Completed · Running · Failed · Pending · Queued`.
- **Forms:** label above field, inline validation tied to the field, keyboard flow, accessible errors that state the fix.
- **States:** loading = content-shaped skeletons (not spinners-over-everything); empty = explain why + the single next action as a button; error = cause + retry + diagnostics. Errors don't apologize; empty screens invite action.
- **Icons:** one line set (Lucide). **No emoji. No mixed styles.**

## Layout shell

Top bar (global search · ⌘K · notifications · connection status · market status · profile · theme) → left nav (grouped: Research · Discovery · Portfolio · Risk · Execution · Analytics · Operations · Settings; collapsible, icons + tooltips, clear active state) → scrollable workspace → optional status strip (connection · data freshness · sync). Desktop-first; nav becomes a drawer and tables scroll horizontally with a frozen first column on mobile.

## Signature experiences (build these — they ARE MarketOS's identity)

Each is grounded in trading's own world and obeys the calm thesis. The "future-level" feel is craft and density, not flash.

1. **Thesis Radar** — the multi-factor conviction visualization (full spec below). The product's signature analysis view.
2. **Living tape ribbon** — a thin, always-present market tape in the status strip; watchlist/index deltas scroll subtly, monospaced, semantic color on numbers only; pauses on hover, click a token to open it. (Displays existing data — frozen-safe.)
3. **Command palette (⌘K) as the spine** — navigate + fuzzy-search symbols + run actions ("Run thesis on RELIANCE", "Open Risk desk") + recent items; also the entry point to the assistant. The fastest path to anything.
4. **PRIME Conviction Gate rail** — a compact vertical 5-gate stepper showing a setup's progress through the validation pipeline; each node passed (`--pos`) / blocked (`--neg`, with the reason inline) / pending (neutral). Visualizes the engine's own logic — IP no competitor has.
5. **Number-morph transitions** — numeric values roll/morph between states (fixed-width tabular) with the 120ms flash. The sacred numerals made kinetic, without flicker.
6. **Session-aware ambient state** — the shell reflects the market session (pre / open / lunch / post / closed): a hairline accent on the top bar plus a session clock counting down to the next boundary (IST). Calm and informative.
7. **Density dial** — one control shifts the whole workspace between Comfortable / Compact / Terminal (row heights, type sizes, padding via token multipliers); persisted. A newcomer stays comfortable; a desk cranks to terminal density.
8. **Provenance ("why") on every signal** — every score, recommendation, and key number expands to its evidence chain (signals, data, timestamp, source). Explainable analysis that doubles as the trust/compliance surface.
9. **Data-freshness & latency pills** — per-panel "as of HH:MM:SS" and a connection pill that turns `--warn` when data is stale/delayed. Honesty about data state is institutional credibility. (Reflects existing connection state — frozen-safe.)
10. **Desk presets (saved layouts)** — users compose multi-panel layouts (Research / Risk / Execution desk) and switch instantly via ⌘K. Makes it feel like an OS, not a page.
11. **Shortcut overlay** — hold `?` to reveal the current screen's keyboard shortcuts (flight-deck style). Signals a pro tool and teaches power use.
12. **Spotlight focus mode** — a shortcut dims all chrome except the active panel (chart or Thesis Radar) for deep work; esc exits. Calm, Raycast-grade.

*Optional further signatures:* **command receipts** (every action emits a quiet, reversible receipt → an auditable action timeline; ties to compliance) and a third **Terminal theme** (high-contrast, ultra-dense variant beyond day/night).

## The Thesis Radar (full build spec)

**Purpose:** show a setup's entire thesis as a multi-factor conviction profile, readable at a glance *against the pass threshold*.

**Layers (outer → inner):**
- Hairline grid rings at 25 / 50 / 75 / 100 (`--border-hairline`); no heavy axis lines.
- Axis labels at each vertex — `11px` tabular caption, `--text-secondary`.
- **Threshold polygon** — the pass line (e.g. 86) drawn as a low-opacity dashed ring-polygon in `--text-tertiary`. *This is the premium device:* the user sees the thesis shape vs the bar to clear.
- **Conviction polygon** — the actual factor scores; fill `--accent` at ~12% opacity, stroke `--accent` 1.5px. A vertex/edge below threshold tints `--neg`; an exceptional factor (≥95) tints `--pos`. Color = information.
- **Confidence band (optional)** — a translucent band between per-factor min/max (e.g. across timeframes) so it reads as analysis, not a single guess.
- **Center** — the composite score (0–100) as the hero number (`24–28px` tabular, `--text-primary`), with the PRIME gate status beneath as a small badge (PASS `--pos` / BLOCKED `--neg` / PENDING neutral).

**Interaction:** hover a spoke → tooltip with the factor label, sub-score, weight, and the top 2–3 evidence signals (this is where thesis + analysis meet the visual). Click a spoke → open that factor's detail panel. Comparison mode → overlay up to 3 symbols' conviction polygons, ghosted, with a monospaced legend. Toggles for threshold / band / labels.

**Motion:** the polygon draws/morphs in over ~250ms (standard easing) on load and on data change; `prefers-reduced-motion` → snap, no animation. No spin or pulse.

**Build:** custom **SVG with `visx` (`@visx/radar`, `@visx/shape`) or d3** — *not* a default Recharts radar (it reads generic). Theme via CSS variables so dark/light works automatically.

**Accessibility:** the radar is augmentation, not the sole source — always render an adjacent compact factor table (label / score / status) for screen readers and colorblind users; add ▲/▼ glyphs at vertices alongside color.

**Data shape (bind to engine output — do not fabricate scores):**
```ts
interface ThesisRadar {
  symbol: string;
  compositeScore: number;          // 0–100 — center hero number
  threshold: number;               // e.g. 86 — reference polygon
  gate: "PASS" | "BLOCKED" | "PENDING";
  factors: Array<{
    key: string;
    label: string;                 // e.g. "Structure"
    score: number;                 // 0–100
    weight?: number;
    band?: [number, number];       // optional min/max for the confidence band
    evidence?: string[];           // top signals, shown in the tooltip
  }>;
}
```
**Default factor axes (map to the engine's real factors; edit to match):** Trend · Momentum · Structure (smart-money) · Liquidity · Volatility · Risk/Reward · Confluence · Conviction. The scores come from the existing scoring engine — read them, never invent or alter them (frozen).

## Voice & copy

Active voice, sentence case, no filler. Name things by what the user controls, not how the system is built. A control says what it does ("Save changes," not "Submit") and keeps that name through the flow ("Publish" → "Published"). Errors are direct about what happened and the fix. Empty states invite action.

## When building, always

- Tokens only — zero hardcoded hex/px in components.
- Dark **and** light both work; theme via CSS variables.
- Every numeral tabular and aligned; lakh/crore option respected.
- Semantic color (`--pos`/`--neg`/`--warn`/`--crit`) on **data only**; the brand accent is never green or red.
- Keyboard-complete + visible focus ring (`--accent`) + reduced-motion respected.
- Virtualize any list/table that can exceed ~100 rows; target 60fps interactions.
- Reuse existing components; never duplicate a button, badge, or panel.
- If a change needs backend / API / DB / auth / architecture edits → **STOP and flag**.

## Anti-patterns (never)

Neon, animated/flashy gradients, glassmorphism overload · oversized rounded cartoon cards · emoji or mixed icon styles · zebra striping · color used as decoration · any animation that draws attention to itself (bounce/pulse/spin) · a floating AI chatbot bubble (the assistant lives in the palette, a slide-over, or inline annotations) · renaming anything back to "Scanner" (AI is an assistant, not the product) · new persistence infra beyond the existing settings store.
