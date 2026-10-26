# Spike: routing between huts (GraphHopper/OSM vs. swissTLM3D)

Date: 2026-10-18. Script: `spikes/tlm_vs_sac.py`, removed, in git history (results in `data/raw/tlm_vs_sac.json`). The GraphHopper setup and script were removed after the decision.

17 hut-to-hut pairs (T2–T5) with an official SAC route time ("From <hut>" routes) were routed by both engines and compared with the SAC values.

## Result

| | GraphHopper 11.1 + OSM + SRTM | swissTLM3D hiking network |
|---|---|---|
| Ascent vs. SAC | 2–10× too high (SRTM noise; e.g. 8479 m vs. 815 m) | Close for most pairs (e.g. 498/580, 823/790, 883/815 m) |
| Route plausibility | Several long detours (25 km vs. about 7 km) | No obvious detours |
| Swiss time / SAC time | median 1.34, range 0.68–11.2 | **median 0.99**, range 0.39–2.86 |
| DIN time / SAC time | median 2.32 | median 1.21 |
| Grade | OSM `sac_scale` T1–T6 (often disagrees with SAC) | Yellow / red-white / blue-white. T2–T3 → all red. T3+…T5 → 4 of 6 blue |
| Setup | Docker, 523 MB OSM extract, 3 GB RAM, ~75 s import | 198 MB download, pure Python (pyogrio + shapely + networkx), ~45 s graph build |

**Decision: use swissTLM3D (official signposted network) as the routing source, with the Swiss hiking-time formula.** GraphHopper/OSM is dropped.

The remaining outliers mostly look like SAC data issues (Bergsee → Voralp: 60 min for 530 m ascent) or different route variants (Sunniggrat → Leutschach: SAC ascent 1000 m vs. 481 m on the direct trail). Calibrating per grade needs more pairs than 17.

## swissTLM3D hiking network

- STAC collection `ch.swisstopo.swisstlm3d-wanderwege`, GeoPackage (`SWISSTLM3D_WANDERWEGE.gpkg`, layer `tlm_strassen_strasse`, LV95, LineString Z). Edition July 2026. Open data (credit swisstopo).
- 409,276 line features: 318,573 `Wanderweg` (yellow), 88,008 `Bergwanderweg` (red-white), 2,695 `Alpinwanderweg` (blue-white). `objektart` gives the path type (1m/2m Weg, Strasse, Markierte Spur, Faehre, ...).
- Lines are split at junctions, so connecting endpoints (1 m precision) gives one connected graph: 388,972 nodes, 818,442 directed edges. All 34 test huts are within 70 m of a node.
- Z values come from the official terrain model, so ascent/descent need no external elevation service.
- Official grade mapping (Schweizer Wanderwege / SAC): yellow ≈ T1, red-white ≈ T2–T3 (sometimes T4), blue-white ≈ T4–T6. A hut's SAC route grade can refine this where available.
- The full swissTLM3D (4.8 GB) also contains unmarked paths. Only needed if some huts turn out to be unreachable on the signposted network.

## Swiss hiking-time formula

- Schweizer Wanderwege, "Wanderzeitberechnung, Version 2020.2" (8 June 2020): minutes per horizontal km = 15th-degree polynomial in slope (%), 4.2 km/h on the flat. Coefficients are confirmed by two independent implementations (via-helvetica `src/metrics/routeMetrics.ts`, openrouteservice forum).
- Applied per ~20 m piece. Beyond ±40 % slope the time grows linearly (SAC "Les Alpes" 2013/05). via-helvetica clamps at 40 % instead.
- Official signpost times for steep or difficult trails are adjusted by hand, so the formula is a lower bound for T4+.

## GraphHopper notes (for reference)

- Tested setup: GraphHopper 11.1 in Docker (6 GB limit), built-in `hike` model with T6 allowed, Geofabrik Switzerland extract. Imported in about 75 s with 3 GB peak.
- SRTM (via srtm.kurviger.de) is the weak point. swisstopo's `profile.json` (swissALTI3D, fair use about 20 req/min) would fix elevation but not the OSM path gaps.
