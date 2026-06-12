# Satellite Validation Candidates

This note selects a small set of flood events for closer validation before any
larger satellite integration work. The goal is to stay aligned with the
multimodal dataset objective: each candidate should be evaluated as an event
record with satellite layers, news/context, quality flags, and limitations.

## Selection Criteria

- The event must be a flood event already present in the project event table.
- Sentinel-2 pre/post change should be usable, unless the event is included as a
  deliberate limitation case.
- Prefer events with low local cloud cover and low cloud/nodata in the
  water-change mask.
- Prefer candidate new water that is not only attached to persistent water,
  coastline, or river edges.
- Include at least one event with both Sentinel-1 and Sentinel-2 available.

## Recommended Candidates

### 1. 2018-0390-QAT - Qatar / Doha

Reason to validate:

- Strongest quantitative candidate in the current ranking.
- Sentinel-1 and Sentinel-2 change layers are both available.
- Sentinel-2 candidate new water is large enough to inspect visually.

Current metrics:

```text
recommended_primary_layer = sentinel-1-and-sentinel-2
s2_candidate_new_water_area_km2 = 3.4729
s2_isolated_new_water_area_km2 = 1.4038
max_s2_local_cloud_cover = 8.96%
```

Known risk:

- The AOI is coastal/lagoon-like, so some signal may come from shoreline,
  tidal/water-level differences, or geometric mismatch rather than flood extent.

Validation focus:

- Check whether the new-water pixels are inland enough to be plausible flood
  water.
- Compare with event news and any available external flood reports.
- Treat as a strong but high-caution candidate.

### 2. 2017-0045-PAK - Pakistan / Balochistan Districts

Reason to validate:

- Best Sentinel-2-only candidate from the current ranking.
- Very low cloud cover.
- Candidate new water appears isolated rather than mostly adjacent to persistent
  water.

Current metrics:

```text
recommended_primary_layer = sentinel-2-change
s2_candidate_new_water_area_km2 = 1.5991
s2_isolated_new_water_area_km2 = 1.5991
max_s2_local_cloud_cover = 0.1587%
```

Known risk:

- Sentinel-1 pre/post change layer is not available, so this is not a complete
  S1/S2 multimodal case.

Validation focus:

- Check whether the Sentinel-2 change mask corresponds to plausible flood water
  in the true-color pre/post images.
- Validate the event with news/context because radar confirmation is missing.
- Use as the cleanest Sentinel-2 spectral-change candidate.

### 3. 2018-0221-SDN - Sudan / Blue Nile and Darfur

Reason to validate:

- Good multimodal candidate with both Sentinel-1 and Sentinel-2 available.
- Low local cloud cover.
- Candidate new water is small but fully isolated in the current context metric.

Current metrics:

```text
recommended_primary_layer = sentinel-1-and-sentinel-2
s2_candidate_new_water_area_km2 = 0.1038
s2_isolated_new_water_area_km2 = 0.1038
max_s2_local_cloud_cover = 1.8311%
```

Known risk:

- The detected flood-like area is small, so it may be less useful as a
  visually impressive example.

Validation focus:

- Check consistency between Sentinel-1 and Sentinel-2 masks.
- Confirm whether a small but clean signal is still useful for the dataset.
- Use as a conservative multimodal example.

## Secondary / Limitation Cases

### 2018-0116-KEN - Kenya / Nairobi

Useful as a small, relatively clean case already inspected earlier.

```text
s2_candidate_new_water_area_km2 = 0.1831
s2_isolated_new_water_area_km2 = 0.1648
max_s2_local_cloud_cover = 3.2898%
```

### 2017-0504-ALB - Albania

Useful mainly as a cautionary case:

```text
s2_candidate_new_water_area_km2 = 1.8188
s2_isolated_new_water_area_km2 = 0.1221
```

The large gap between total and isolated new water suggests that much of the
signal is close to persistent water or affected by nodata.

### 2016-0334-GRC - Greece

Useful mainly as a coastal/persistent-water ambiguity example:

```text
s2_candidate_new_water_area_km2 = 0.8850
s2_isolated_new_water_area_km2 = 0.0793
```

## Validation Checklist

For each primary candidate:

1. Read the event metadata in `manifest.json`.
2. Inspect `s2_pre_true_color.png` and `s2_post_true_color.png`.
3. Inspect `s2_water_change_mask.png`.
4. Inspect `s1_change_mask.png` when available.
5. Compare with event location/date and news/context.
6. Assign a validation label:
   - `good_candidate`;
   - `usable_with_caution`;
   - `ambiguous`;
   - `reject`.
7. Record the reason, especially cloud/nodata, coastline, persistent water, or
   missing modality.
