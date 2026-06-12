# Satellite Validation Review

Validation note for the three selected flood candidates. This is a first manual
review of satellite layers, not a final ground-truth validation.

Local input folder:

```text
C:\Users\saver\Desktop\DisastriNuovi
```

Generated visual overview:

```text
results/satellite/_validation_review/validation_candidates_contact_sheet.png
```

## Label Legend

- `good_candidate`: useful for the multimodal dataset with manageable caveats.
- `usable_with_caution`: technically useful, but has a clear limitation that must
  be documented.
- `ambiguous`: signal is too difficult to interpret without stronger external
  validation.
- `reject`: not useful for the current validation goal.

## Summary

| Event | Country | Recommended Label | Main Reason |
| --- | --- | --- | --- |
| `2018-0221-SDN` | Sudan | `good_candidate` | Clean multimodal case, small but coherent S1/S2 signal. |
| `2017-0045-PAK` | Pakistan | `usable_with_caution` | Strong Sentinel-2 signal, but missing S1 pre/post confirmation. |
| `2018-0390-QAT` | Qatar | `usable_with_caution` | Strongest signal, but coastal/lagoon context may produce artefacts. |

## Candidate 1: 2018-0221-SDN - Sudan

Event metadata:

```text
country = Sudan
location = Blue Nile State | Westenr Darfur | Zalingei
start_date = 2018-06-18
recommended_primary_layer = sentinel-1-and-sentinel-2
```

Available layers:

```text
Sentinel-1 pre/post = available
Sentinel-1 change mask = available
Sentinel-2 pre/post true color = available
Sentinel-2 MNDWI masks = available
Sentinel-2 water-change mask = available
```

Quality and statistics:

```text
s2_pre_local_cloud_cover = 0.0%
s2_post_local_cloud_cover = 1.8311%
s1_candidate_area_km2 = 0.1160
s2_candidate_new_water_area_km2 = 0.1038
s2_unreliable_cloud_percent = 1.8478%
```

Visual review:

- The Sentinel-2 pre/post images show a visible seasonal/vegetation change, but
  cloud cover is low.
- The water-change mask contains a small number of isolated candidate new-water
  pixels.
- Sentinel-1 is available and gives this case a complete S1/S2 multimodal
  structure.

Label:

```text
good_candidate
```

Reason:

This is not visually spectacular, but it is the cleanest multimodal validation
case among the current candidates. It is useful for testing whether the pipeline
can keep a conservative, quality-controlled event.

Open validation task:

- Verify whether the event location/date is supported by news or external flood
  reports.

## Candidate 2: 2017-0045-PAK - Pakistan

Event metadata:

```text
country = Pakistan
location = Pishin | Gwadar | Panjgur | Kech | Qila Abdullah districts | Ziarat
start_date = 2017-01-17
recommended_primary_layer = sentinel-2-change
```

Available layers:

```text
Sentinel-1 pre/post = incomplete
Sentinel-1 change mask = not available
Sentinel-2 pre/post true color = available
Sentinel-2 MNDWI masks = available
Sentinel-2 water-change mask = available
```

Quality and statistics:

```text
s2_pre_local_cloud_cover = 0.0%
s2_post_local_cloud_cover = 0.1587%
s2_candidate_new_water_area_km2 = 1.5991
s2_unreliable_cloud_percent = 0.1404%
```

Visual review:

- The AOI is arid and mostly cloud-free, making Sentinel-2 interpretation easier.
- The water-change mask highlights compact candidate new-water areas.
- The signal is much cleaner than the coastal/persistent-water cases.
- The main limitation is the missing Sentinel-1 change layer.

Label:

```text
usable_with_caution
```

Reason:

This is the best current Sentinel-2 spectral-change candidate. It is highly
useful for testing optical water-change extraction, but it is not a full S1/S2
multimodal example because Sentinel-1 pre/post change is missing.

Open validation task:

- Check news/context for the January 2017 Balochistan flood event.
- Decide whether an S2-only satellite layer is acceptable for the multimodal
  dataset, as long as other modalities such as news and weather are available.

## Candidate 3: 2018-0390-QAT - Qatar

Event metadata:

```text
country = Qatar
location = Doha
start_date = 2018-10-20
recommended_primary_layer = sentinel-1-and-sentinel-2
```

Available layers:

```text
Sentinel-1 pre/post = available
Sentinel-1 change mask = available
Sentinel-2 pre/post true color = available
Sentinel-2 MNDWI masks = available
Sentinel-2 water-change mask = available
```

Quality and statistics:

```text
s2_pre_local_cloud_cover = 0.4822%
s2_post_local_cloud_cover = 8.96%
s1_candidate_area_km2 = 0.7629
s2_candidate_new_water_area_km2 = 3.4729
s2_unreliable_cloud_percent = 9.1995%
s2_isolated_new_water_area_km2 = 1.4038
```

Visual review:

- This is the strongest event by total Sentinel-2 candidate new-water area.
- The true-color imagery clearly includes coastal/lagoon water and built-up
  areas around Doha.
- Part of the water-change signal may represent shoreline, lagoon, water-level,
  tidal, or registration effects rather than floodwater.
- Sentinel-1 is available, which makes the event worth keeping for validation.

Label:

```text
usable_with_caution
```

Reason:

This is the best stress-test candidate: high signal and full S1/S2 availability,
but it needs external validation before being presented as a reliable flood mask.

Open validation task:

- Validate against event news or official flood reports for Doha around
  2018-10-20.
- Inspect whether the candidate new-water pixels are inland enough to support a
  flood interpretation.

## Recommendation

Use the candidates in this order:

1. `2018-0221-SDN` as the conservative multimodal case.
2. `2017-0045-PAK` as the clean Sentinel-2 change case.
3. `2018-0390-QAT` as the high-signal but high-caution case.

For the professor, these three events can be presented as a validation set rather
than a final dataset subset. The next decision is whether incomplete satellite
modalities are acceptable event-by-event, or whether the dataset should require
both Sentinel-1 and Sentinel-2 layers for every retained event.
