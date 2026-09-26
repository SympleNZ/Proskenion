# Self-hosted fonts

Both families are served from this directory by nginx (spec §21.1) and never
fetched from Google Fonts at runtime — the AV VLAN may have no internet access.
WOFF2, Latin subset, variable weight axis, hinting stripped. Total 26,992 bytes,
under the 35 KB budget in §21.1.

| File | Family | Weight axis | Glyphs | Size |
| --- | --- | --- | --- | --- |
| `dm-sans-latin.woff2` | DM Sans | `wght` 400–700 (variable) | Basic Latin, Latin-1 Supplement, dashes, quotes, bullet, ellipsis, minus, arrows | 18,880 B |
| `jetbrains-mono-latin.woff2` | JetBrains Mono | `wght` 400–500 (variable) | Basic Latin, °, ·, ×, dashes, minus, ellipsis, arrows | 8,112 B |

## Source

Fetched on 2026-09-09 from the Google Fonts CSS API `latin` unicode-range
files (variable fonts, WOFF2):

- DM Sans v17 — `https://fonts.gstatic.com/s/dmsans/v17/rP2Yp2ywxg089UriI5-g4vlH9VoD8Cmcqbu0-K4.woff2`
- JetBrains Mono v24 — `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwg.woff2`

Then reduced with fontTools (`uv run --with fonttools --with brotli`):
`varLib.instancer` narrows the `wght` axis to the weights we use (DM Sans
400–700, JetBrains Mono 400–500); `pyftsubset` keeps only the code points
above, the `kern` and `tnum` layout features, and the copyright and licence
name records (IDs 0 and 7), with `--no-hinting --desubroutinize
--flavor=woff2`. JetBrains Mono's code ligatures (`calt`, `liga`) are dropped
— it carries technical values (§21.3), not source code.

Upstream projects: <https://github.com/googlefonts/dm-fonts> and
<https://github.com/JetBrains/JetBrainsMono>.

## Licence

Both families are licensed under the SIL Open Font License, Version 1.1
(OFL-1.1). The OFL permits bundling, redistribution and subsetting of the
fonts provided they are not sold on their own and the copyright notices are
retained; the notices remain in each file's `name` table.

- DM Sans: Copyright 2014 The DM Sans Project Authors
  (https://github.com/googlefonts/dm-fonts)
- JetBrains Mono: Copyright 2020 The JetBrains Mono Project Authors
  (https://github.com/JetBrains/JetBrainsMono)

Full licence text: <https://openfontlicense.org/open-font-license-official-text/>
