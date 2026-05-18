import pandas as pd
import json
import os

def merge_and_clean_datasets(emdat_path, gdis_path):
    """
    Reads the EM-DAT and GDIS datasets, performs a merge based on the cleaned ID, and calculates the time windows.
    """
    # Load the datasets
    df_emdat = pd.read_excel(emdat_path)
    df_gdis = pd.read_csv(gdis_path)
    
    # EM-DAT ID Cleaning: removes the final extension to align with GDIS format (e.g. 2000-0123-USA -> 2000-0123)
    df_emdat['disasterno_clean'] = df_emdat['DisNo.'].str.split('-').str[:2].str.join('-')
    
    # INNER JOIN: keeps only records present in both datasets to ensure the presence of coordinates
    df_merged = pd.merge(df_emdat, df_gdis, left_on='disasterno_clean', right_on='disasterno', how='inner')
    
    # Drop rows with missing date components to ensure we can create a valid start_date
    df_merged = df_merged.dropna(subset=['Start Year', 'Start Month', 'Start Day'])
    
    #Create the start_date column by combining Year, Month, and Day into a native datetime type
    df_merged['start_date'] = pd.to_datetime(
        df_merged[['Start Year', 'Start Month', 'Start Day']].astype(int).astype(str).agg('-'.join, axis=1)
    )
    
    #Calculates the required time window for analysis (-10 days and +10 days from the event)
    df_merged['date_minus_10'] = (df_merged['start_date'] - pd.Timedelta(days=10)).dt.strftime('%Y-%m-%d')
    df_merged['date_plus_10'] = (df_merged['start_date'] + pd.Timedelta(days=10)).dt.strftime('%Y-%m-%d')

    #Standardizes the disaster type column name to 'disaster_type' for consistency in the final dataset
    df_merged = df_merged.rename(columns={'Disaster Type': 'disaster_type'})
    
    # Select and return only the necessary columns, ready for the next pipeline step
    df_selected = df_merged[[
        'disasterno', 'disaster_type', 'latitude', 'longitude', 
        'start_date', 'date_minus_10', 'date_plus_10'
    ]].copy()
    
    # Convert the start_date column to string for consistency in the final JSON
    df_selected['start_date'] = df_selected['start_date'].dt.strftime('%Y-%m-%d')
    
    return df_selected


def load_checkpoint(file_path):
    """
    Initializes or recovers the state of the final dataset from the hard drive.
    If the file already exists (interrupted session), it loads it; otherwise, it returns an empty dictionary.
    """
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            # If the file is corrupted or empty, return an empty dictionary to avoid blocking the application
            return {}
    return {}


def save_checkpoint(data, file_path):
    """
    Writes the current state of the data to the output JSON file.
    Called at each iteration or block to ensure data persistence.
    """
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)