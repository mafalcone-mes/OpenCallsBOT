# Oppo's drawings

Put Oppo here as SVG files, one per pose. All are optional: a missing pose
uses `default.svg`, and with no `default.svg` the app shows a plain
placeholder mark.

| file | where it appears |
|---|---|
| `default.svg` | side bar and phone bar logo (28 px), Home hero (96 px, on the green: `currentColor` becomes the dark ink and `var(--oppo)` parts the light panel colour) |
| `searching.svg` | refresh running (32 px, gently pulses) |
| `found.svg` | refresh finished (32 px) |
| `empty.svg` | empty pages: feed, discovery, hidden, studios (96 px) |
| `sleep.svg` | "Never refreshed" / "Last refreshed N days ago" (20 px) |
| `favicon.svg` | browser tab icon (else `default.svg`) |

Tips:

- Use a square `viewBox` (e.g. `0 0 96 96`); width and height in the file are
  ignored, the app sets them.
- Paint the outline/body with `currentColor` (ink in light mode, near white in
  dark) and the accent parts with `style="fill: var(--oppo)"` (radar green),
  so Oppo follows both themes. Other fixed colours stay as drawn.
- Keep it readable at 20 px: the small poses need a simple silhouette.
