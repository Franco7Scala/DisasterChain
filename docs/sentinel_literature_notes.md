# Sentinel Literature Notes

Fonte: archivio locale `drive-download-20260610T104549Z-3-001.zip`, con 10
articoli su flood mapping, Sentinel-1, Sentinel-2 e fusione SAR/ottico.

Queste note servono a tradurre la letteratura in requisiti pratici per la
pipeline del progetto.

## Articoli Nell'Archivio

- `isprs-archives-XLIII-B3-2020-641-2020.pdf`  
  Fusion approach Sentinel-1/Sentinel-2 per flood mapping con Random Forest.
- `1-s2.0-S003442572500286X-main.pdf`  
  Benchmark/modelli deep learning su Sentinel-1, Sentinel-2 e dataset flood.
- `remotesensing-12-02073-v2.pdf`  
  Rapid flood mapping con Sentinel-1 SAR, Sentinel-2 optical, supervised classifier e change detection.
- `s12524-021-01487-3.pdf`  
  Identificazione flood extent e paddy rice affected fields in Bihar usando Sentinel-1/Sentinel-2 in GEE.
- `1-s2.0-S0048969721066638-main.pdf`  
  Sardoba dam break, Random Forest, Sentinel-1/Sentinel-2, GLCM, PCA, water/vegetation indices.
- `OmbriaNetSupervised_Flood_Mapping_via_Convolutional_Neural_Networks_Using_Multitemporal_Sentinel-1_and_Sentinel-2_Data_Fusion.pdf`  
  Dataset OMBRIA e CNN per segmentazione flood con dati multitemporali e multimodali.
- `nhess-22-2473-2022.pdf`  
  Studio su osservabilita' Sentinel-1/Sentinel-2 degli eventi flood in Europa.
- `1-s2.0-S0924271621002227-main.pdf`  
  Analisi diversita' Sentinel-1/Sentinel-2 per flood inundation mapping, U-Net, Sen1Floods11.
- `1-s2.0-S2352938523000290-main.pdf`  
  Bangladesh, Random Forest, Sentinel-1 per flood inundation, Sentinel-2 per land cover/damage assessment.
- `ijgi-12-00053-v4.pdf`  
  Mozambique, Sentinel-1 multitemporale, differenza pre/post, Otsu thresholding, validazione con Copernicus EMS.

## Pattern Ricorrenti

### Sentinel-1

- Usato come sorgente principale durante o subito dopo l'evento perche' vede
  attraverso nuvole e pioggia.
- Input ricorrenti:
  - GRD;
  - polarizzazioni VV e VH;
  - correzione terreno/ortorettifica;
  - conversione backscatter in dB;
  - coppie pre/post evento.
- Output tipici:
  - immagini SAR pre/post;
  - differenza o rapporto pre/post;
  - flood mask tramite soglia manuale, Otsu o classificatore;
  - mappa di cambiamento.

### Sentinel-2

- Usato per validazione visuale, estrazione land cover, training sample selection
  e indici spettrali.
- Input ricorrenti:
  - L2A quando disponibile;
  - RGB true color;
  - false color;
  - NDWI/MNDWI;
  - NDVI;
  - SCL o cloud mask.
- Limite principale:
  - copertura nuvolosa durante gli eventi flood.

### Fusione SAR/Ottico

- Approccio molto comune:
  - Sentinel-1 per mappare acqua/flood durante l'evento;
  - Sentinel-2 per interpretabilita', land cover e supporto al training;
  - classificatori Random Forest o reti CNN/U-Net per combinare feature.
- In diversi lavori Sentinel-2 migliora il contesto, ma Sentinel-1 resta
  indispensabile quando l'ottico e' nuvoloso.

## Output Che Dovremmo Mirare A Produrre

### Output minimi per evento

- `manifest.json` con:
  - evento;
  - bbox;
  - date;
  - scene Sentinel selezionate;
  - cloud cover;
  - percorsi output.
- Sentinel-1:
  - pre-event VV/VH;
  - post-event VV/VH;
  - change mask radar.
- Sentinel-2:
  - true color pre/post;
  - MNDWI/NDWI pre/post;
  - cloud mask;
  - water mask.

### Output intermedi da aggiungere

- Statistiche per ogni maschera:
  - percentuale acqua;
  - percentuale nuvola;
  - percentuale nodata;
  - area stimata in km2.
- Local cloud cover nell'AOI, non solo `eo:cloud_cover` tile-level.
- Maschera acqua permanente, o almeno differenza `post_water - pre_water`
  quando entrambe le immagini S2 sono pulite.
- Soglia automatica Otsu per Sentinel-1 change detection.
- Possibile filtro DEM/slope per ridurre falsi positivi SAR in zone ripide.

### Output finali da dataset

- Flood extent candidate mask.
- Flood confidence o quality flags:
  - S1 available;
  - S2 pre usable;
  - S2 post usable;
  - local cloud percentage;
  - permanent water contamination risk.
- Collegamento tra evento, news, meteo e layer satellitari.

## Implicazioni Per La Pipeline Attuale

La pipeline appena costruita e' allineata alla fase iniziale della letteratura:

- scarica S1 pre/post;
- scarica S2 pre/post;
- produce MNDWI/NDWI;
- usa SCL per mascherare nuvole;
- produce manifest e output per evento.

I gap principali rispetto agli articoli sono:

- selezione migliore della scena Sentinel-2 usando nuvolosita' locale;
- stima area in km2;
- water change mask vera, non solo water mask post-evento;
- rimozione o separazione dell'acqua permanente;
- soglia automatica o classificatore per Sentinel-1;
- validazione con ground truth esterna, ad esempio Copernicus EMS, Sen1Floods11
  o dataset tipo OMBRIA.
