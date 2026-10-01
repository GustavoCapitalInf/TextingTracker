# PhoneTracker interface

The latest user preference is a deliberately plain office utility: white backgrounds, neutral gray controls, and no dark mode, green branding, theme selector, decorative dashboard, or elaborate navigation. This replaces the earlier bank-tool styling.

## Shared layout

Use one compact, wrapping header containing the product name, ordinary navigation links, the signed-in name, the admin's Account link, and Sign out. No icon rail, sidebar, drawer, or navigation toggles. Page content is headings, forms, assignment rows, and tables. Reps see read-only lists for today and a plain seven-day schedule. Calendar entries show list title, texting group and count; future dates contain no phone numbers or active list links. Login is a simple form.

## Color and typography

Use the fixed light palette in static/tracker/tokens.css. Buttons are light gray with gray borders and readable dark-gray text. Reserve pale yellow for duplicate or previous-upload warnings and red for errors. Neutral styling covers all other statuses. No shadows, gradients, pill buttons, avatars, remote fonts, or decorative illustrations.

Use local system sans text and local monospace phone numbers. Keep headings modest and controls conventional. Focus indicators must be clearly visible. Root tokens.css is a portable copy of the active tokens.

## Interaction and access

Do not store or restore theme preferences. Phone numbers and page data must never be stored in the browser. Preserve login, role and ownership checks, today's date restriction, CSRF, duplicate handling, privacy covers, stale-list monitoring, and audit logging. Clear withdraws all rep access while retaining history. No call outcomes, interest controls, progress bars, metrics dashboards, or list exports.

Header links wrap at narrow widths, with no hidden menu. At 320, 375, 414, and 768px, content must fit the viewport; wide tables may scroll in their own containers. Motion is unnecessary.
