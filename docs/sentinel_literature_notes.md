# Sentinel Literature Notes

Source: local archive `drive-download-20260610T104549Z-3-001.zip`, containing
10 papers on flood mapping, Sentinel-1, Sentinel-2, and SAR/optical data fusion.

These notes translate the literature into practical requirements for the project
pipeline.

## Papers In The Archive

- `isprs-archives-XLIII-B3-2020-641-2020.pdf`  
  Sentinel-1/Sentinel-2 fusion approach for flood mapping with Random Forest.
- `1-s2.0-S003442572500286X-main.pdf`  
  Benchmark/deep learning models using Sentinel-1, Sentinel-2, and flood
  datasets.
- `remotesensing-12-02073-v2.pdf`  
  Rapid flood mapping with Sentinel-1 SAR, Sentinel-2 optical imagery,
  supervised classification, and change detection.
- `s12524-021-01487-3.pdf`  
  Flood extent and affected paddy rice field identification in Bihar using
  Sentinel-1/Sentinel-2 in Google Earth Engine.
- `1-s2.0-S0048969721066638-main.pdf`  
  Sardoba dam break, Random Forest, Sentinel-1/Sentinel-2, GLCM, PCA, and
  water/vegetation indices.
- `OmbriaNetSupervised_Flood_Mapping_via_Convolutional_Neural_Networks_Using_Multitemporal_Sentinel-1_and_Sentinel-2_Data_Fusion.pdf`  
  OMBRIA dataset and CNN-based flood segmentation with multitemporal and
  multimodal data.
- `nhess-22-2473-2022.pdf`  
  Study on Sentinel-1/Sentinel-2 observability of flood events in Europe.
- `1-s2.0-S0924271621002227-main.pdf`  
  Sentinel-1/Sentinel-2 diversity analysis for flood inundation mapping,
  U-Net, and Sen1Floods11.
- `1-s2.0-S2352938523000290-main.pdf`  
  Bangladesh case study with Random Forest, Sentinel-1 for flood inundation,
  and Sentinel-2 for land-cover/damage assessment.
- `ijgi-12-00053-v4.pdf`  
  Mozambique case study with multitemporal Sentinel-1, pre/post differencing,
  Otsu thresholding, and validation with Copernicus EMS.

## Recurring Patterns

### Sentinel-1

- Used as the main source during or shortly after the event because it can
  observe through clouds and rain.
- Recurring inputs:
  - GRD;
  - VV and VH polarizations;
  - terrain correction/orthorectification;
  - backscatter conversion to dB;
  - pre/post-event pairs.
- Typical outputs:
  - pre/post-event SAR images;
  - pre/post difference or ratio;
  - flood mask through manual thresholding, Otsu thresholding, or a classifier;
  - change map.

### Sentinel-2

- Used for visual validation, land-cover extraction, training sample selection,
  and spectral indices.
- Recurring inputs:
  - L2A when available;
  - true color RGB;
  - false color;
  - NDWI/MNDWI;
  - NDVI;
  - SCL or cloud mask.
- Main limitation:
  - cloud cover during flood events.

### SAR/Optical Fusion

- Very common approach:
  - Sentinel-1 maps water/flooding during the event;
  - Sentinel-2 provides interpretability, land cover, and training support;
  - Random Forest or CNN/U-Net classifiers combine features.
- In several works, Sentinel-2 improves contextual interpretation, while
  Sentinel-1 remains essential when optical imagery is cloudy.

## Target Outputs

### Minimum Outputs Per Event

- `manifest.json` with:
  - event;
  - bbox;
  - dates;
  - selected Sentinel scenes;
  - cloud cover;
  - output paths.
- Sentinel-1:
  - pre-event VV/VH;
  - post-event VV/VH;
  - radar change mask.
- Sentinel-2:
  - pre/post true color;
  - pre/post MNDWI/NDWI;
  - cloud mask;
  - water mask.

### Intermediate Outputs To Add

- Statistics for each mask:
  - water percentage;
  - cloud percentage;
  - nodata percentage;
  - estimated area in km2.
- Local cloud cover inside the AOI, not only tile-level `eo:cloud_cover`.
- Permanent water mask, or at least `post_water - pre_water` difference when
  both Sentinel-2 images are clean.
- Automatic Otsu thresholding for Sentinel-1 change detection.
- Possible DEM/slope filter to reduce SAR false positives in steep areas.

### Final Dataset Outputs

- Candidate flood extent mask.
- Flood confidence or quality flags:
  - S1 available;
  - S2 pre usable;
  - S2 post usable;
  - local cloud percentage;
  - permanent water contamination risk.
- Link between event, news, weather, and satellite layers.

## Implications For The Current Pipeline

The initial pipeline is aligned with the first stage of the literature:

- downloads S1 pre/post imagery;
- downloads S2 pre/post imagery;
- produces MNDWI/NDWI;
- uses SCL to mask clouds;
- produces event-level manifests and outputs.

The main gaps compared with the papers are:

- better Sentinel-2 scene selection using local cloudiness;
- area estimation in km2;
- true water change mask, not only post-event water mask;
- removal or separation of permanent water;
- automatic thresholding or classifier for Sentinel-1;
- validation with external ground truth, for example Copernicus EMS,
  Sen1Floods11, or OMBRIA-like datasets.
