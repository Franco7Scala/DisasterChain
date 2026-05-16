import pandas as pd
import requests

#Load clean dataset with disasters and their dates
df = pd.read_csv("disastri_per_satellite.csv")

#Select the first row of the dataset to test the API call
riga = df.iloc[0]

#Extract relevant information from the selected row
url = "https://archive-api.open-meteo.com/v1/archive"

#Create a dictionary with the parameters for the API call
parametri = {
    "latitude": riga['latitude'],
    "longitude": riga['longitude'],
    "start_date": riga['Data_Meno_10'],
    "end_date": riga['Data_Piu_10'],
    #Ask for daily data on precipitation and temperature
    "daily": ["rain_sum", "snowfall_sum", "temperature_2m_max", "temperature_2m_min"],
    "timezone": "auto"
}

#Make the API call and store the response
risposta = requests.get(url, params=parametri)

#Transform the response into a JSON object
dati_json = risposta.json()

print("--- ISPEZIONE RISPOSTA API ---")
print("Codice di stato HTTP:", risposta.status_code)

if risposta.status_code == 200 and 'daily' in dati_json:
    # Se il server risponde OK, convertiamo la sezione 'daily' in una tabella
    df_meteo = pd.DataFrame(dati_json['daily'])
    print("\n--- DATI METEO GIORNALIERI RICEVUTI ---")
    print(df_meteo)
else:
    # Se c'è ancora un errore, stampiamo la risposta grezza per capire perché
    print("\nErrore nella richiesta. Risposta del server:")
    print(dati_json)
