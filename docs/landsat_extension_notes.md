# Landsat Extension Notes

This note tracks the first investigation about extending the multimodal
satellite pipeline to events before the Sentinel period.

## Motivation

The current multimodal satellite pipeline is based on Sentinel data:

- Sentinel-1 GRD for SAR VV/VH;
- Sentinel-2 L2A for optical/false-color/raw bands;
- Sentinel-3 SLSTR for thermal source bands;
- land cover as a static contextual layer.

This works well for recent events, but it limits the useful temporal coverage of
the final dataset. Landsat can potentially cover many older events while keeping
similar general data types: optical bands, false color composites, quality/cloud
metadata, and thermal bands.

## Main Finding

There are two slightly different cases to keep separate:

- Copernicus Data Space Sentinel Hub documentation lists Landsat 8-9 L1, but the
  CDSE availability is documented from January 2021.
- The general Sentinel Hub documentation lists additional Landsat collections on
  the US-West deployment (`services-uswest2.sentinel-hub.com/api`), including
  Landsat 8-9, Landsat 7, and Landsat 4-5.

So the next practical step is not yet a full integration in the pipeline. First
we need to verify whether our current authentication setup can access the
US-West Sentinel Hub endpoint, or whether we need a separate Sentinel Hub setup
outside the current Copernicus Data Space OAuth client.

## Candidate Collections

| Mission | Useful period | Product | Data type id | Notes |
| --- | --- | --- | --- | --- |
| Landsat 8-9 OLI-TIRS | from 2013 for L8, from 2022 for L9 | Level 2 | `landsat-ot-l2` | Best first candidate: optical surface reflectance plus surface temperature. |
| Landsat 7 ETM+ | from 1999 | Level 2 | `landsat-etm-l2` | Useful for pre-2013 events; has thermal data, but scan-line issues can affect scenes after 2003. |
| Landsat 4-5 TM | 1982-2012 | Level 2 | `landsat-tm-l2` | Useful for older events; band set is less modern but still comparable for RGB/NIR/SWIR/thermal. |
| Landsat 1-5 MSS | 1972-1992 | Level 1 | listed in Sentinel Hub data docs | Lower priority because the band set is less comparable to Sentinel-2/Landsat TM. |

## Mapping To Current Outputs

The Landsat layer should not replace Sentinel outputs. It should be an optional
fallback/extension for older events.

| Current output concept | Landsat equivalent | Caveat |
| --- | --- | --- |
| True color image | RGB bands | Band names differ by mission. |
| False color image | SWIR/NIR/Red or NIR/Red/Green composite | Need one documented choice for consistency. |
| Raw optical bands | Blue, Green, Red, NIR, SWIR1, SWIR2 | Landsat has fewer bands than Sentinel-2. |
| Thermal source data | Landsat thermal / surface temperature bands | Available in L2 products, but native thermal resolution is coarser and resampled. |
| Cloud/quality metadata | `eo:cloud_cover`, QA bands | Local cloud quality still needs care, as with Sentinel-2. |

## Resolution Choice

For Landsat, the most natural common grid is 30 m:

- optical reflective bands are generally delivered at 30 m;
- thermal bands are coarser natively but are delivered/resampled in the products;
- using 20 m would create artificial upsampling compared with the source data.

This means the dataset schema may need a `source_family` or `sensor_group`
field, because Sentinel base layers are currently saved at 20 m while Landsat
base layers would likely be saved at 30 m.

## Proposed Minimal Test

Before implementing a full batch layer:

1. Add configurable Sentinel Hub base URLs so the code can point either to CDSE
   or to the US-West endpoint.
2. Test a single Landsat 8-9 L2 Catalog search on a 2013-2014 event.
3. If authentication works, download one small true-color TIFF and one raw-bands
   TIFF for that event.
4. Only after that, add a standard Landsat folder under the multimodal output
   structure.

The first probe command is intentionally catalog-only:

```bash
python src/probe_landsat_endpoint.py --event-id <event-id> --collection landsat-ot-l2
```

For a dry run without API calls:

```bash
python src/probe_landsat_endpoint.py --event-id <event-id> --collection landsat-ot-l2 --dry-run
```

If the US-West deployment rejects the current credentials, the script should
fail before any pipeline integration work is attempted.

Expected tentative structure:

```text
results/multimodal_satellite/<event_id>/
  manifest.json
  satellite/
    landsat/
      YYYY-MM-DD/
        true_color.tif
        false_color.tif
        raw_bands.tif
        thermal_bands.tif
        true_color_preview.png
        false_color_preview.png
```

## Open Questions

- Can the current Copernicus OAuth client authenticate against the US-West
  Sentinel Hub deployment?
- If not, do we need separate Sentinel Hub credentials?
- Should Landsat be used only for pre-Sentinel events, or also as an additional
  source for recent events?
- Should Landsat outputs live in a separate `landsat/` folder, or should they be
  normalized into the same daily sensor slots as Sentinel?

## Sources

- Sentinel Hub data deployments and Landsat availability:
  https://docs.sentinel-hub.com/api/latest/data/
- Landsat 8-9 L2:
  https://docs.sentinel-hub.com/api/latest/data/landsat-8-l2/
- Landsat 7 ETM+ L2:
  https://docs.sentinel-hub.com/api/latest/data/landsat-etm-l2/
- Landsat 4-5 TM L2:
  https://docs.sentinel-hub.com/api/latest/data/landsat-tm-l2/
- Copernicus Data Space Landsat 8-9 L1:
  https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/Landsat8-9.html
