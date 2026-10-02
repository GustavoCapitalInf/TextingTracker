# PhoneTracker interface

The current direction is a dark operations console, drawn from ObsidianUI, Arc UI, SpaceUI, Bencho and the Agent Builder integrations screen: charcoal surfaces, hairline borders, large rounded cards, white pill primary actions, and color used only for meaning. It replaces the earlier plain white utility look. Dark is the default; a light appearance and an Auto setting (follow the device) are available from the Dark / Light / Auto switch.

## Shared layout

A fixed left sidebar holds the brand mark, the navigation (icon plus label; managers see Lead lists, Upload list, Calendar Management, Templates, Texting lists, Accounts and History; reps see Your leads and Templates), the Dark / Light / Auto switch, and a user card with Account (admins only) and Sign out. The sign-in page shows the same switch in its top bar. A slim sticky top bar shows the section name, the demo chip when relevant, and today's company date. Page content sits in a centered column of at most 1360px. Below 900px the sidebar becomes a top block: brand, then the icon-only appearance switch and user actions, then navigation as wrapping pills. There is no hidden menu and no JavaScript navigation.

Page heads use a large, tightly tracked title with a muted one-line description and actions on the right. Content is cards (`panel`), tables inside cards, stat tiles, and rows. Wide tables scroll inside their card on phones; a rep's phone list never scrolls sideways.

## Calendar

Seven rounded day tiles. Today has a soft blue ring and a Today label; the manager's selected day is slightly lighter. Each list is a small nested card with a compact group name (GFS · Donut, Ringcentral · Clean), the list name, counts, and a dotted status (Sent, Available today, Upcoming). Planned entries from Calendar Management (weekly plans) use a dashed outline, a "Planned" prefix, an Upload link and Skip this day. Rep calendars never show planned entries or numbers for other days. Below 768px days stack as rows with the date on the left.

## Templates page

Text templates are cards: the name, the exact message in a soft inset box that keeps line breaks, a character count, and a **Copy text** button that appears only when the browser can copy (it briefly reads "Copied"). Admins also get an Edit link on each card and an "Add a template" panel beside the list; reps see the cards alone, full width, with a larger copy button on phones. Empty states tell admins to add the first template and tell reps their admin hasn't added any yet.

## Color and typography

All colors are tokens in static/tracker/tokens.css (root tokens.css is a portable copy). Surfaces step from page (#0b0b0c) to card to nested panel to hover. Primary actions are white pills with dark text; secondary actions are dark pills with a light border; destructive actions are red-tinted pills. Status chips are pills with a leading dot: green for available, active and published; amber for drafts and previously uploaded numbers; red for errors and invalid rows; neutral otherwise. Links and focus rings use a soft blue. Every text pair meets WCAG AA (4.5:1) against its surface, including the faint label color, in both appearances.

The light appearance uses cool, blue-leaning whites and grays (page #f3f4f6, cards #ffffff); never cream, beige or warm paper tones, and no surface may have more red than blue. Primary actions flip to near-black pills with white text. Status hues are darker for AA on white, and status fills are solid near-white tints (a see-through amber over the gray page turns beige). Light tokens sit under `:root[data-theme="light"]` and are repeated for `data-theme="system"` inside `prefers-color-scheme: light`; the two copies must stay identical and cover every dark color token (a test checks this). Components use tokens only; the one exception is the dialog backdrop, which repeats each theme's scrim because older Chromium cannot read tokens there.

Type uses the local system sans stack (SF Pro, Segoe UI, Inter if installed) with tight tracking on headings and tabular figures for counts. Phone numbers use the local monospace stack. No remote fonts, scripts, images or CDNs.

## Interaction and access

The appearance choice is the only stored preference: a `phonetracker_theme` cookie (dark, light or system; HttpOnly, SameSite=Lax, one year) that the server reads to render `<html data-theme>`, so the first paint is already correct and no inline script is needed. The switch posts a normal form; with JavaScript it applies at once and saves in the background without reloading the page. Phone numbers and page data must never be stored in the browser. Preserve login, role and ownership checks, today's date restriction, CSRF, duplicate handling, the previously-uploaded Yes/No dialog, privacy covers, stale-list monitoring, and audit logging. Clear withdraws all rep access while retaining history. No call outcomes, interest controls, progress bars, metrics dashboards, or list exports.

Motion is limited to short color and press transitions and is removed under reduced-motion. Focus indicators are always visible. Content must fit 320, 375, 390, 768, 1024 and 1440px without sideways page scrolling. Printing shows only a notice.
