---
name: Uno Notetaker
description: A calm list of meetings by day and a clean notes document, in the category-standard Granola / Fathom manner, with one indigo accent.
colors:
  accent: "oklch(0.55 0.22 273)"
  accent-dark: "oklch(0.68 0.18 273)"
  accent-ink: "#ffffff"
  page: "#ffffff"
  page-dark: "#161618"
  sidebar: "#f7f7f8"
  sidebar-dark: "#1b1b1e"
  raised: "#ffffff"
  raised-dark: "#202024"
  sunk: "#f3f3f5"
  sunk-dark: "#232327"
  ink: "#18181b"
  ink-dark: "#ececee"
  ink-2: "#3f3f46"
  ink-2-dark: "#c9c9cf"
  muted: "#6b6b75"
  muted-dark: "#a0a0aa"
  faint: "#9a9aa3"
  faint-dark: "#75757f"
  line: "#e7e7ea"
  line-dark: "#2c2c31"
  line-soft: "#f0f0f2"
  line-soft-dark: "#242428"
  ok: "#18794e"
  ok-soft: "#e9f6ef"
  warn: "#8a5a00"
  warn-soft: "#fdf6e3"
  bad: "#c62a2f"
  bad-soft: "#fdeeee"
  rec: "#e5484d"
  speaker-0: "#2f5bea"
  speaker-1: "#8a4bd6"
  speaker-2: "#1f8a4c"
  speaker-3: "#c2562f"
  speaker-4: "#a8327a"
  speaker-5: "#2a7f96"
typography:
  page-title:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI Variable Text, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
    fontSize: "30px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.028em"
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI Variable Text, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
    fontSize: "26px"
    fontWeight: 680
    lineHeight: 1.5
    letterSpacing: "-0.022em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI Variable Text, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
    fontSize: "16.5px"
    fontWeight: 650
    lineHeight: 1.35
    letterSpacing: "-0.012em"
  reading:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI Variable Text, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
    fontSize: "15.5px"
    fontWeight: 400
    lineHeight: 1.68
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI Variable Text, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI Variable Text, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.5
  mono:
    fontFamily: "ui-monospace, SF Mono, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
rounded:
  sm: "7px"
  md: "10px"
  lg: "14px"
  dialog: "16px"
  pill: "99px"
spacing:
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "22px"
  page-x: "48px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.sm}"
    height: "34px"
    padding: "0 14px"
  button-default:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    height: "34px"
    padding: "0 14px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.sm}"
    height: "34px"
    padding: "0 14px"
  button-big:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.md}"
    height: "44px"
    padding: "0 22px"
  input:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    height: "36px"
    padding: "0 11px"
  search:
    backgroundColor: "{colors.line-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    height: "34px"
    padding: "0 10px 0 32px"
  panel:
    backgroundColor: "{colors.raised}"
    rounded: "{rounded.lg}"
  sidebar-item:
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "7px 10px 8px"
  view-tab:
    textColor: "{colors.ink-2}"
    rounded: "{rounded.sm}"
    height: "32px"
    padding: "0 10px"
  segmented-tab:
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
    height: "28px"
    padding: "0 12px"
  chip:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "8px 13px"
  starter-card:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "14px"
  dialog:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.dialog}"
    padding: "22px"
  toast:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.page}"
    rounded: "{rounded.md}"
    padding: "10px 16px"
---

# Design System: Uno Notetaker

## Overview

**Creative North Star: "The Plain Meeting Notebook"**

This is the category standard played straight. A white page, a quiet gray sidebar holding the meetings list by day, and a single reading column for the notes. The person came back after a call to read, tick an action item, share, or ask a question; the interface stays out of the way of that. One indigo accent (Uno Work's own) marks the primary action, the selected state in forms, and the current line in a transcript. Everything else is ink and gray.

Density is calm and Operate-mode: 14px chrome, 15.5px notes. Surfaces are white on white separated by hairlines and very soft shadows, not by colored fills. Light and dark both ship; dark follows the system (`prefers-color-scheme`) and is a full token remap, not an inversion filter. The product is aimed at non-developers, so the plumbing (providers, models, keys) never appears in the main chrome.

**Key Characteristics:**
- White page, `#f7f7f8` sidebar, ink `#18181b`; one indigo accent used on rare, meaningful marks.
- One system UI sans for everything, weight and tracking doing the hierarchy; mono only in the raw-notes editor and inline code.
- Hairline borders first, shadow second; shadows are soft and blurred, never hard-offset.
- 1.75px-stroke inline SVG icons from one sprite, 16px, `currentColor`.
- Sentence-case labels everywhere; no uppercase, no tracking-widened labels.
- Status color is semantic and sparse: green done, amber due/warning, red error, a separate red for the live recording dot.

## Colors

Restrained palette: neutral tints carrying roughly nine-tenths of every screen, one indigo accent, four semantic colors. Values live in `:root` and are remapped inside one `prefers-color-scheme: dark` block; the frontmatter lists each token's light value and `-dark` counterpart.

### Primary
- **Uno Indigo** (`oklch(0.55 0.22 273)` light, `oklch(0.68 0.18 273)` dark): the primary button, checked checkboxes and switches, the progress bar, the user's chat bubble, and the active spinner arc. Derived tints: a 10% mix into the page for selected-row and current-transcript-line fill (`accent-soft`), and an 88% mix toward black (80% toward white in dark) for accent-colored text and links (`accent-text`). Text on solid indigo is white.

### Neutral
- **Page White** (`#ffffff` / `#161618`): main canvas and the base for all translucent mixes.
- **Quiet Side Gray** (`#f7f7f8` / `#1b1b1e`): the sidebar, settings navigation rail, and login backdrop.
- **Raised** (`#ffffff` / `#202024`): panels, inputs, menus, dialogs, active list item. Equals the page in light; lifts slightly in dark.
- **Sunk** (`#f3f3f5` / `#232327`): segmented-tab track, assistant chat bubble, day separators, consent and file-name blocks, code, progress track.
- **Ink** (`#18181b` / `#ececee`): primary text; also the fill of the audio play button, scrubber and toasts.
- **Ink 2** (`#3f3f46` / `#c9c9cf`): secondary text, nav labels, field labels.
- **Muted** (`#6b6b75` / `#a0a0aa`): metadata, helper lines, inactive tabs.
- **Faint** (`#9a9aa3` / `#75757f`): placeholders, chevrons, decorative icons. Recorded as carried: it is also used for transcript and note timestamps, where it falls under comfortable contrast on white. Use `muted` for any text a person must read.
- **Line / Line Soft** (`#e7e7ea` / `#2c2c31`, `#f0f0f2` / `#242428`): outer borders and in-panel row dividers respectively. Hover and pressed fills are 4.5% and 8% ink overlays (`hover`, `press`).

### Semantic
- **Done Green** (`#18794e` / `#5bd197`, soft `#e9f6ef`), **Due Amber** (`#8a5a00` / `#f0c060`, soft `#fdf6e3`), **Error Red** (`#c62a2f` / `#ff7b7e`, soft `#fdeeee`): status text and their soft tinted backgrounds for notices, due pills and error boxes.
- **Record Red** (`#e5484d`): the live-recording dot, the "now" chip, and the live count. Kept distinct from Error Red.
- **Speaker palette** (six hues, `speaker-0` to `speaker-5`, lighter variants in dark): speaker names, avatar faces and the talk-time bar. Assigned by speaker order.

### Named Rules
**The One Indigo Rule.** Indigo marks an action or a selection, never decoration. A screen carries at most a primary button, its checked controls, and the current transcript line in indigo.

**The Hairline Before Shadow Rule.** Separate regions with a 1px `line` or `line-soft` border; reach for a shadow only for things that float or are being pressed.

**The Tint, Don't Invent Rule.** New states are derived with `color-mix` from existing tokens (`accent-soft`, `warn-soft`); do not add a new hue for a new state.

## Typography

**Display, Body and Label Font:** the platform system UI stack (`-apple-system`, `BlinkMacSystemFont`, `Segoe UI Variable Text`, `Segoe UI`, `Roboto`, `Helvetica Neue`, `Arial`, sans-serif), with `font-feature-settings: "cv11", "ss01"`.
**Mono:** `ui-monospace, SF Mono, Menlo, Consolas`, only for the raw-notes editor and inline `code`.

**Character:** neutral and native, so it feels at home inside the Uno Work frame. Hierarchy is carried by weight (520 to 700), negative tracking on larger sizes, and size steps, not by a second family. This is the build's single face; see the not-canonized note below.

### Hierarchy
- **Page title** (700, 30px, 1.2, -0.028em): the editable meeting title; 24px on phone.
- **Headline** (680 to 690, 26 to 28px, -0.022em to -0.025em): page headings and the Home greeting; 24px on phone.
- **Title** (650, 16.5px, 1.35, -0.012em): section headings inside rendered notes and the action-item block.
- **Section** (640, 15px, -0.01em): `sec-head` headings such as "Coming up"; 18px (660) for dialog titles.
- **Reading** (400, 15.5px, 1.68): the notes document. Transcript and action text are 15px / 1.55 to 1.6.
- **Body** (400, 14px, 1.5): sidebar, rows, forms.
- **Label** (400 to 600, 12 to 12.5px): metadata, timestamps, day headers ("Today", sentence case, weight 560, muted), counts. Numbers use tabular figures.

### Named Rules
**The Sentence Case Rule.** Every label, day header and section title is sentence case at its natural width. No uppercase, no widened tracking, no small-caps captions.

**The Reading Column Rule.** Long text sits in a 760px column (672px of text after 48px padding) at 15.5px/1.68; chrome stays at 14px or below.

## Layout

A two-column app shell: a 272px sidebar (`--side-w`) and a fluid main area, full dynamic viewport height, each column scrolling independently. The sidebar stacks brand, "New" menu button, search, three view tabs (Meetings, Action items, Calls with counts), then the meeting list grouped by day. The main column centers a single 760px column (`.page` and `.meeting`, 44px or 18px top, 48px sides, 120 to 140px bottom to clear the floating player). A sticky 52px translucent top bar gains a hairline and a breadcrumb only once the page has scrolled.

Spacing rhythm is a 4/6/8/12/16/22 ladder: 6 to 8px inside compact controls, 12 to 16px for row padding and panel gaps, 22px for card and dialog padding, 36px between page sections, 28 to 30px above headings in notes. Rows inside a panel are 11 to 14px tall-padded and divided by `line-soft`. Two-column form rows use a 12px gap.

Responsive: at 760px the layout becomes a single column that swaps between list and detail (`view-list` / `view-detail` on `body`), view tabs turn into a horizontal scroller, page padding drops to 18px, grids (starters, form rows, settings) collapse to one column, settings becomes a full-screen sheet, and a fixed bottom bar carries Record and Upload with safe-area padding. Hover-only affordances (the "Ask Uno" pill on action items) become always visible where hover is unavailable.

## Elevation & Depth

Hybrid, leaning flat: depth is mostly tonal (white on gray sidebar, sunk fills) and hairline borders, with three soft blurred shadows reserved for floating or interactive surfaces. Dark mode keeps the same three roles with heavier black alpha.

### Shadow Vocabulary
- **Sm** (`0 1px 2px rgb(16 16 24 / 0.06)`): buttons, the active list item, the New button, the selected segmented tab, logo.
- **Default** (`0 1px 2px rgb(16 16 24 / 0.05), 0 4px 14px -4px rgb(16 16 24 / 0.10)`): the Ask input, the recording card, starter cards on hover, screen video.
- **Large** (`0 2px 6px rgb(16 16 24 / 0.06), 0 24px 60px -12px rgb(16 16 24 / 0.28)`): popover menus, dialogs, the floating player pill, toasts.
- Translucency: the top bar (88% page, 10px blur), player bar (92% raised, 12px blur) and phone bottom bar use backdrop blur so content passes beneath.

### Named Rules
**The Float-Only Rule.** Large shadow means "above the page" (menu, dialog, player, toast). A panel that sits in the flow gets a border, not a large shadow.

## Shapes

Gently rounded, never sharp, never fully bubbly except where a pill is the honest form. Controls use 7px (`sm`); menus, large buttons and choice rows 10px (`md`); panels, starter cards and progress boxes 14px (`lg`); dialogs 16px; the recording card 18px. Pills (99px) are for chips, due dates, the Ask-Uno button, the player and speaker chips. Chat bubbles are 14px with one square 5px tail corner. Avatars and play button are circles. Checkboxes are 17px with 5px radius. Borders are 1px hairlines; focus is a 2px indigo outline at 70% with 2px offset, plus a 3px 14% indigo halo on text fields.

## Components

### Buttons
- **Shape:** 34px tall, 7px radius, 14px side padding, medium weight (540); 28px small; 44px big at 10px radius.
- **Default:** raised fill, 1px line border, Sm shadow; hover darkens 6% toward ink; press nudges 0.5px down.
- **Primary:** indigo fill, white text, a faint inset top highlight; hover mixes 10% black. One per view or dialog.
- **Ghost / icon / link:** transparent with hover fill; icon buttons are 32px squares; links are indigo text with an underline on hover. Danger is red text only, not a red fill.

### Chips and pills
- **Suggestion chip:** pill, raised fill, line border; hover tints to `accent-soft` with an indigo-leaning border. **Speaker chip:** pill with a 20px colored face. **Due pill:** amber on `warn-soft`. **Platform tag** (Meet, Zoom, Teams): a 6px square swatch plus muted 12px text.

### Cards / Containers
- **Panel:** raised, 14px radius, 1px line border, overflow hidden, rows separated by `line-soft`; no shadow. Day separators inside a panel are sunk bands.
- **Starter card:** 3-up grid of panels with a 18px indigo icon, bold title and muted line; hover adds the Default shadow.
- **Recording card:** centered, 18px radius, a 46px light-weight tabular timer, a pulsing record dot, and meter bars.

### Inputs / Fields
- 36px tall, 7px radius, 1px line border on raised; focus shifts border to a 50% indigo mix and adds the 3px halo. Selects use a chevron data-URI. The search field is borderless on a hover-tint fill and lifts to raised on focus. The meeting title is a borderless input styled as a heading. Choice rows are bordered 10px cards that tint to `accent-soft` when checked. Switches are 32x19 pills that fill indigo when on.

### Navigation
- **View tabs:** 32px rows, icon plus label plus right-aligned count; selected is a pressed-gray fill with ink text, not indigo. **Segmented tabs** (Notes, Transcript, Ask) sit in a sunk track with a raised selected thumb. **Settings** is a dialog with a left rail of the same row style. **Top bar** is sticky and translucent.

### Meeting list item
- Title at weight 520 on one truncated line, then a 12.5px muted line with icons and tabular time; active item is raised with Sm shadow and an inset hairline. Search results show timestamped snippets, with matches in a yellow `mark`.

### Event row (Coming up)
- Three columns: bold tabular start time over a faint end time; name with a muted sub line (platform, guests); a join switch. Past rows fade to 62%; calls the bot will skip show muted.

### Notes document and action items
- Rendered markdown at 15.5px with indigo-free structure: bold labels, faint list markers, small timestamp links that turn indigo on hover. Action items are custom 17px indigo checkboxes with a drawn check, struck-through faint text when done, an optional amber due pill, and a hover-revealed "Ask Uno" pill.

### Dialogs, menus, toasts
- Dialogs: 480px, 16px radius, Large shadow, 22px padding, right-aligned actions; an `<details>` "more" fold hides advanced fields. Menus are 10px-radius popovers with 7px 10px items and a short scale-in. Toasts are ink pills at the bottom center; the error variant is solid red.

### Floating player
- A 560px pill pinned 16px above the bottom: ink circular play button, 15-second skip icons, tabular times, a 4px ink scrubber, speed chip.

### Motion
One easing, `cubic-bezier(0.16, 1, 0.3, 1)`, at 150 to 300ms for hover, popover entrance (4px rise, 98% scale), dialog entrance (6px rise), switch and checkbox. The only looping motions are the record dot pulse and the spinner. `prefers-reduced-motion` collapses all durations.

## Do's and Don'ts

### Do:
- **Do** keep indigo to the primary action, checked controls and the current transcript line (The One Indigo Rule).
- **Do** use `muted` for any text a person has to read, and `faint` only for placeholders and decorative marks.
- **Do** write labels in sentence case and keep page-section titles at 15px, weight 640.
- **Do** build new surfaces from the existing set: panel, row, chip, button, dialog; separate with `line-soft`.
- **Do** use the sprite icons (16px, 1.75px stroke, `currentColor`) and tabular numerals for times and counts.
- **Do** ship both themes by adding any new color as a token with a dark value in the `prefers-color-scheme` block.
- **Do** keep provider, model and key vocabulary out of the main chrome; it belongs behind the Advanced fold in settings.

### Don't:
- **Don't** add a second accent hue or a gradient; derive tints with `color-mix` from existing tokens.
- **Don't** use hard offset shadows or large shadows on in-flow panels.
- **Don't** introduce uppercase or letter-spaced micro-labels above headings; the build has none and new surfaces should not start.
- **Don't** reach for glyph or emoji icons; add a symbol to the sprite.
- **Don't** use a colored border-stripe on cards or a colored fill for the selected sidebar row (it is a gray press tint).
- **Don't** style a destructive action as a solid red button inside a flow; red text, with a confirm, is the pattern.
- **Don't** inherit the third-party brand colors (Meet green, Zoom blue, Teams violet) as system colors; they are tags for outside products only.
