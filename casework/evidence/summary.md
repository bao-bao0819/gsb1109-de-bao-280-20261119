# Per-page evidence summary

| page | classifier label | image coverage | text chars | amount tokens in text layer | plugin status | entries |
|---|---|---|---|---|---|---|
| 1 | scan_without_text | 1.0 | 0 | - | no_usable_amount_text | 0 |
| 2 | scan_with_text | 1.0 | 34 | - | no_usable_amount_text | 0 |
| 3 | digital | 0.0 | 233 | 1,250.00 | layout_mismatch | 0 |
| 4 | digital | 0.0 | 128 | 48.20, -130.75 | extracted | 2 |

Plugin status meanings:

- `no_usable_amount_text`: text layer has no amount token; any amount on the page exists only in raster pixels (conversion layer gap).
- `layout_mismatch`: an amount token is in the text layer but the row does not match the plugin's date/description/amount template (plugin rule rejection).
- `extracted`: entries emitted; see entries.beancount.
