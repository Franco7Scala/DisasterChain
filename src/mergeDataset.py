import pandas as pd

df_emdat = pd.read_excel("public_emdat_dal_2000.xlsx")
df_gdis = pd.read_csv("pend-gdis-1960-2018-disasterlocations.csv")

df_emdat['disasterno_clean'] = df_emdat['DisNo.'].str.split('-').str[:2].str.join('-')

df_unito = pd.merge(df_emdat, df_gdis, left_on='disasterno_clean', right_on='disasterno', how='inner')

df_unito = df_unito.dropna(subset=['Start Year', 'Start Month', 'Start Day'])

df_unito['Data_Inizio'] = pd.to_datetime(df_unito[['Start Year', 'Start Month', 'Start Day']].astype(int).astype(str).agg('-'.join, axis=1))

df_unito['Data_Meno_10'] = df_unito['Data_Inizio'] - pd.Timedelta(days=10)
df_unito['Data_Piu_10'] = df_unito['Data_Inizio'] + pd.Timedelta(days=10)

df_selezionato = df_unito[['disasterno', 'Disaster Type', 'latitude', 'longitude', 'Data_Inizio', 'Data_Meno_10', 'Data_Piu_10']]

df_selezionato.to_csv("disastri_per_satellite.csv", index=False)