# Spike: hut-reservation.org (HRS) API — findings

Date: 2026-10-18. Answers brief open question 3.
Sources: `domwoe/hut-reservation-mcp` (MIT; `src/upstream.ts`, `src/types.ts`, `data/catalog.json`,
local clone in `reference/hut-reservation-mcp`), `KlaraSaary/find-free-huts` (AGPL, endpoints only, no code copied),
`miloszpp/sac-hut-checker`, plus 14 live unauthenticated requests (script: `spikes/hut_reservation_probe.py`,
raw responses: `data/raw/hrs/`).

## TL;DR

- **Availability is public.** No login, no cookies, no CSRF token needed. This includes the POST endpoints.
- **Best endpoint for us: `GET /api/v1/reservation/getHutAvailability?hutId=X&step=WIZARD`.** One call returns the
  per-day `freeBeds` for the whole booking window of a hut (181 to 1000 days from today). It is not in the MCP repo;
  the HRS web app's calendar view uses it. It maps directly to our `availability` table.
- **Hut info is public:** `GET /api/v1/reservation/hutInfo/{id}` gives name, coordinates, altitude, total beds,
  bed categories with capacities, website, warden, phone, booking rules text and a picture URL.
- **No prices anywhere** in the public responses (no price/tariff fields). Lauteraarhütte's description says
  half board is included and payment is "Bar in CHF", but gives no amounts. So `hut_price` stays manual or scraped.
  Prices may only show up later in the booking flow (`preBook`), which is out of scope.
- **Hut list requires login** (`/api/v1/manage/hutsList` returns 401 for anonymous users, per the MCP source).
  Workaround: the MCP repo's bundled `data/catalog.json` (MIT, 567 huts, 250 CH, refreshed 2026-06-22) has
  every `hutId`. We could also probe `hutInfo/{id}` over an id range slowly, if we ever need to.

## Endpoints

Base URL: `https://www.hut-reservation.org/api/v1`. Send `Accept: application/json`. POST bodies are JSON.
Dates in requests and in `dateFormatted` use the Swiss format `DD.MM.YYYY`.

| Purpose | Method + path | Auth | Notes |
|---|---|---|---|
| Season calendar | `GET /reservation/getHutAvailability?hutId={id}&step=WIZARD` | none | **Use this.** Returns a JSON array with one object per day. 33–210 KB. |
| Hut info | `GET /reservation/hutInfo/{id}` | none | Static metadata, about 3–5 KB. |
| Bed categories for a period | `POST /reservation/hutStatus/{id}` body `{"arrivalDate","departureDate"}` | none | Returns `categories[]` (categoryID, totalSleepingPlaces, reservationMode) and sometimes `hutStatus` and waiting-list flags. 400 if the period is closed or not released. |
| Exact-period availability | `POST /reservation/checkAvailability/{id}` | none | Body (copied from the MCP): `arrivalDate, departureDate, numberOfPeople:0, nextPossibleReservations:false, peoplePerCategory:[{categoryId, people:0}], hutId, serviced, isWaitingListEnabled, isWaitingListAccepted, allowAnyBedCategory:false`. Returns `availabilityPerDayDTOs[]`. The MCP calls `hutStatus` first to get the category ids. |
| CSRF | `GET /csrf` → `{token}` + `XSRF-TOKEN` cookie | — | Only needed for authenticated calls (sent as the `X-XSRF-TOKEN` header). Not needed for the public calls above. |
| Hut list | `GET /manage/hutsList` | **login** | Not called. Returns `[{hutId, hutName, hutCountry}]`. |
| Login and booking | `/users/login`, `/reservation/preBook`, `PUT /reservation/submit`, `myReservations`, `reservationSummary/{id}`, `getPartialCancellationFee` | login | **Out of scope, not called.** SAC accounts log in through SAC SSO (cookies `SESSION` + `XSRF-TOKEN`). |

Other projects:
- `find-free-huts` uses the same `hutsList`, `hutInfo` and `checkAvailability` endpoints, with browser session cookies.
- `sac-hut-checker` uses Puppeteer to scrape the legacy `alpsonline.org/reservation/calendar?hut_id=` page.
  It is outdated and not useful to us.

## Response schemas (trimmed)

`getHutAvailability` (an array, one element per day, starting today):
```json
{"freeBedsPerCategory": {"106": 120, "1756": 0}, "freeBeds": 120, "hutStatus": "SERVICED",
 "date": "2027-10-31T00:00:00Z", "dateFormatted": "31.10.2027", "totalSleepingPlaces": 120,
 "percentage": "AVAILABLE"}
{"freeBedsPerCategory": {}, "freeBeds": null, "hutStatus": "CLOSED", "date": "2029-07-13T00:00:00Z",
 "dateFormatted": "13.07.2029", "totalSleepingPlaces": 0, "percentage": "CLOSED"}
```
- `hutStatus` values seen: `SERVICED`, `UNSERVICED` (winter room only), `CLOSED`.
- `percentage` is a label, not a number. Values seen: `AVAILABLE`, `NEARLY FULL` (at or below about 17% free),
  `FULL`, `CLOSED`.

`checkAvailability` → `availabilityPerDayDTOs[]`:
```json
{"day": "24.10.2026", "freePlaces": 12, "hutStatus": "UNSERVICED", "dayOfWeek": "SATURDAY",
 "availableForReservation": true,
 "bedCategoriesData": [{"hutBedCategoryId": 1756, "serviceMode": "UNSERVICED", "totalPlaces": 12,
   "totalFreePlaces": 12, "hutBedCategoryLanguagesData": [...], "freeOnGuestsNumber": true}]}
```

`hutInfo` keys: `hutId, hutName, tenantCode ("SAC"), tenantCountry, coordinates ("lat, lon" string), altitude
("2883 m" string), totalBedsInfo ("120" string), hutBedCategories[] (categoryID, totalSleepingPlaces,
reservationMode ROOM|UNSERVICED, labels in 4 languages), hutWebsite, hutWarden, phone, picture.blobPath,
hutGeneralDescriptions[] (HTML, per language), maxNumberOfNights, waitingListEnabled, hutUnlocked, providerName,
hutLanguages, supportLink`.

Error format (HTTP 400): `{"messageId": 609, "description": "Arrival date in hut closing period", "param1": "30.10.2027",
"messages": {"EN": "closed or not yet available for booking.", ...}}`. Also seen:
`600 "Reservation deadlines criteria are not matched"` for a date beyond the hut's booking window.

## Mapping to our schema

- `availability(hut_id, date, free_beds, fetched_at)`: one row per element of `getHutAvailability`.
  `date` comes from `dateFormatted` (convert to ISO) and `free_beds` from `freeBeds`.
  Store `NULL` (unknown) when `hutStatus == CLOSED`, or when `totalSleepingPlaces == 0`.
  Worth adding columns `hut_status` and `total_places`, so that "full" (0 of 120) can be told apart from
  "not released" (0 of 0) and "winter room only" (UNSERVICED, small capacity).
- Hut link: we need a `hrs_hut_id` on `hut`. Match SAC huts to the catalog by name and coordinates.
  The ids are small ints, e.g. Monte Rosa 6, Blüemlisalp 1, Britannia 9, Lauteraar 558.
- `hut` metadata: `totalBedsInfo` and per-category capacities are a good source of bed counts.
  `hutWebsite`, `maxNumberOfNights`, the booking-deadline text and the picture URL are optional extras.
- `hut_price`: **nothing available.** Keep `source = 'manual' | 'scraped'`.

## Rate limits and caching

- Responses send `Cache-Control: no-cache, no-store` and come through Azure Front Door (`x-cache: PRIVATE_NOSTORE`).
  There is no server-side caching hint, so we must cache ourselves.
- No rate-limit headers were seen, and no 429 in 14 requests at 1.5 s spacing.
- The MCP queries many huts concurrently, which we should not copy.
- Suggested polling for 250 CH huts: one calendar GET per hut, at most once a day, ≥2 s apart.
  That is about 10 minutes per day. Fetch on demand only for huts in a candidate tour. `hutInfo` at most monthly.
- No cookies are set on public calls.

## Gotchas

- **Booking windows are per hut.** Lauteraar's calendar ends on 16.04.2027 (181 days), Blüemlisalp's is 731 days,
  Monte Rosa's is 1000 days.
- **Summer 2027 is not bookable yet.** Monte Rosa shows `CLOSED` for summer 2027. Blüemlisalp shows `SERVICED`
  but with 0 total and 0 free (`percentage: CLOSED`), and `checkAvailability` returns `freePlaces: 0` with
  `availableForReservation: true`. A naive reader would conclude "full". Always check that the capacity is above 0.
- **Winter rooms show up as availability.** In October, Monte Rosa shows 12 free (`UNSERVICED`, the emergency room).
  Filter on `hutStatus == SERVICED` unless the user wants winter-room tours.
- There are odd single-day records, e.g. Monte Rosa `SERVICED` with 120 free on 31.10.2027 inside a closed period.
  Treat isolated days with caution.
- **HRS coordinates can be wrong.** Blüemlisalphütte is given as `46.3036, 7.4617`; it is actually near 46.49, 7.77.
  Prefer SAC or OSM coordinates. Altitude and bed counts are strings.
- `hutInfo` includes warden names and phone numbers. Don't store these beyond the raw dumps, which are gitignored.
- HTML entities and tags appear in the descriptions.
- `getHutAvailability` is undocumented. We don't know what `step=WIZARD` does; it's copied from the SPA pattern.
  Isolate the client and keep the raw JSON.
- Terms: HRS is a third-party system (operated for SAC/DAV/ÖAV). Read only, low rate, private use.

## Remaining unknowns

- When summer 2027 contingents open. Likely winter or spring; re-probe Blüemlisalp and Monte Rosa in Jan–Mar 2027
  to see a real SERVICED calendar with partial occupancy.
- Whether `getHutAvailability` works without `step`, or with other values.
- Whether any public endpoint exposes prices. Not found; possibly only in the authenticated booking wizard.
- Whether there is an unauthenticated hut list. Probably not; use the MCP catalog or slow id probing.
- What the actual rate limits or WAF thresholds are.
