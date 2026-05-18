import pandas as pd
import requests
import time
import json

#Load the dataset with disasters and their dates
df_disastri = pd.read_csv("../results/disastri_per_satellite.csv")

#Select the first 500 rows of the dataset for testing
#To use the entire dataset, simply comment out the following line
df_test = df_disastri[:500]

#Initialize an empty list to store the weather data for all disasters
dati_meteo_totali = []

#Initialize a cache to store the weather data for already processed locations and date ranges to avoid redundant API calls
meteo_cache = {}

url = "https://archive-api.open-meteo.com/v1/archive"

print(f"Inizio download controllato per i primi {len(df_test)} eventi...")

#Start the loop on the selected rows of the dataset
#itertuples() optimizes the iteration by returning named tuples, which are faster than iterrows()
for indice, riga in enumerate(df_test.itertuples()):

    #Create a unique key for the cache based on the latitude, longitude, and date range of the current row
    chiave_cache = (riga.latitude, riga.longitude, riga.Data_Meno_10, riga.Data_Piu_10)

    if chiave_cache in meteo_cache:
        
        #If the weather data for the current location and date range is already in the cache, retrieve it from there instead of making a new API call
        meteo_giorno = meteo_cache[chiave_cache].copy()

        #Associate the current disaster code (even if the coordinates are the same, the ID is different)
        meteo_giorno['disasterno'] = riga.disasterno

        #Save in the final dataset and continue to the next iteration without making an API call
        dati_meteo_totali.append(meteo_giorno)
        continue

    #Configure the parameters for the API call using the information from the current row
    parametri = {
        "latitude": riga.latitude,
        "longitude": riga.longitude,
        "start_date": riga.Data_Meno_10,
        "end_date": riga.Data_Piu_10,
        "daily": ["rain_sum", "snowfall_sum", "temperature_2m_max", "temperature_2m_min"],
        "timezone": "auto"
    }

    #Error handling for the API call
    try:
        risposta = requests.get(url, params=parametri)

        #If the request was successful, extract the daily data and add it to the list of total weather data
        if risposta.status_code == 200:
            dati_json = risposta.json()

            #if the expected data is present in the response, create a DataFrame with the daily data and add it to the list of total weather data
            if 'daily' in dati_json:
                meteo_giorno =  dati_json['daily']

                #Save the clean data returned by the API using a unique key
                meteo_cache[chiave_cache] = meteo_giorno.copy()

                #Attach the disaster code to the weather data for later merging with the disaster dataset
                meteo_giorno['disasterno'] = riga.disasterno

                #Save the 21 days of weather data for the current disaster in the list of total weather data
                dati_meteo_totali.append(meteo_giorno)

        else:
            print(f"Riga {indice} (ID: {riga.disasterno}): Errore nella richiesta. Codice di stato HTTP: {risposta.status_code}")
        
    except Exception as e:
        print(f"Errore di rete imprevisto alla riga {indice}: {e}")
        
    #Monitor the progress of the loop and print a message every 50 iterations to keep track of the progress
    if indice % 50 == 0 and indice > 0:
        print(f"Progresso: elaborati {indice}/{len(df_test)} eventi.")
    
    #Rate limiting: wait for 100 milliseconds before making the next API call to avoid overwhelming the server
    time.sleep(0.1)

#Save the total weather data for all disasters in a JSON file for later use in merging with the disaster dataset
with open("../results/dataset_meteo_giornaliero_test.json", "w") as f:
    json.dump(dati_meteo_totali, f)

print("\n---OPERAZIONE COMPLETATA---")
print("File di test 'dataset_meteo_giornaliero_test.json' creato con i dati meteo giornalieri per i primi 500 eventi.")