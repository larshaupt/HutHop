# Spike: SAC route portal API

Date: 2026-10-18. Script: `spikes/sac_fetch_huts.py`, removed, in git history. Raw data: `data/raw/sac/` (gitignored).

## Endpoints

Base `https://www.suissealpine.sac-cas.ch/api/1/`. No auth, `Cache-Control: max-age=1800`.
Found by reading the route portal's JS chunks (`/assets_*/js/async/{0,2,6,9,30}.*.min.js`).

| Endpoint | Notes |
|---|---|
| `GET poi/search` | Hut list. **Pagination: pass `cursor=<value from previous response>`** (`offset`/`page` are ignored). `limit=100` works → 4 requests for all huts. |
| `GET poi/search_count` | Same params, returns `{"count": 384}`. |
| `GET poi/search?mode=only_ids` | IDs only. |
| `GET sacplus/poi/{id}?output_lang=en` | **Per-hut detail.** Same fields as search plus `used_as_destination_in`, `used_as_waypoint_in`, `used_as_neighbor_summit_in`, `protection_areas`, `created`, `modified`. |
| `GET route/search?id=1,2&lang=en&output_lang=en` | Route metadata by id (title, `main_difficulty`, `destination_poi`, `availability`). **No geometry, no times.** |
| `GET route/{id}/` | 404 without login (route editor endpoint). |
| `GET protection_area/{id}/published` | Wildlife protection areas (not tested). |

Other things the portal does: filters huts by `hrs_id=` (a list it gets from hut-reservation.org availability), filters "catered in month X" with `catering.month_X=0,1`, and calls `api3.geo.admin.ch` (`ch.bav.haltestellen-oev`) for public transport stops.

**Route geometry is not available** without login, and probably not at all for paid content. The tour graph has to come from our own routing (swissTLM3D, see `routing_findings.md`). SAC times, grades and ascent from `routes[]` (embedded in each hut) are used for calibration and as overrides.

## Opening / catering codes (open question 1): resolved

Both use **0 = yes (whole month), 1 = partly (part of the month), 2 = no**.

- `catering`: hut is catered (staffed). Confirmed by the portal code (`catering.month_X=0,1` = "catered") and by hut-reservation.org: Blüemlisalp `catering` 222221000122 matches HRS `SERVICED` from June 22 to October.
- `opening`: **the unstaffed/self-service part is open** (winter room or self-catering hut). Blüemlisalp 0 all year (winter room always open). Lauteraar 2 in Jul/Aug (winter room closed while staffed). Mutthorn 2 all year (no winter room). This is a strong hypothesis, not confirmed by SAC.
- So: **hut usable on a date** = `catering ∈ {0,1}` or `opening ∈ {0,1}`. **Self-catering** = `opening ∈ {0,1}` (and/or `services.cooking_non_catered`). For `1` (partly), check exact dates in `opentext` or hut-reservation.org.
- 82 of 384 huts are 2/2 all year: the data is missing, not "always closed". Treat them as unknown.

## Dataset stats (Oct 2026)

- 384 huts, all `type=hut`. 199 `is_private=true` (non-SAC-owned huts listed in the portal). 219 have `hrs_id > 0`.
- Jul/Aug: 245 catered, 211 with self-service open.
- `sleeps` missing for 15.
- `geom` is LV95 (E 2.50–2.83 M, N 1.08–1.24 M), so convert with pyproj EPSG:2056 → 4326.
- `routes[]`: 924 total (608 mountain_hiking), 714 `free`, 210 `limited` (paid, **must not be used**).
- `services` most common: drinks, plain_meals, half_board (227), cooking_non_catered (187), not_paid, dogs_on_request, payment_*.
- No prices anywhere.

## Hut-to-hut SAC routes

About 30 hiking routes are titled "From <other hut>". Matching on names finds them (with some false matches, e.g. "Salei" vs "Saleinaz"). These are used in `tlm_vs_sac.py` for calibration. Route times are often only `ascent_time_max` (min is null).
