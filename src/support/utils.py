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
    
    # INNER JOIN: match the same disaster and country to avoid mixing
    # locations from multi-country events.
    df_merged = pd.merge(
        df_emdat,
        df_gdis,
        left_on=['disasterno_clean', 'ISO'],
        right_on=['disasterno', 'iso3'],
        how='inner'
    )
    
    # Drop rows with missing date components to ensure we can create a valid start_date
    df_merged = df_merged.dropna(subset=['Start Year', 'Start Month', 'Start Day'])
    
    #Create the start_date column by combining Year, Month, and Day into a native datetime type
    df_merged['start_date'] = pd.to_datetime(
        df_merged[['Start Year', 'Start Month', 'Start Day']].astype(int).astype(str).agg('-'.join, axis=1)
    )
    
    #Calculates the required time window for analysis (-10 days and +10 days from the event)
    df_merged['date_minus_10'] = (df_merged['start_date'] - pd.Timedelta(days=10)).dt.strftime('%Y-%m-%d')
    df_merged['date_plus_10'] = (df_merged['start_date'] + pd.Timedelta(days=10)).dt.strftime('%Y-%m-%d')

    #Standardizes the selected columns for consistency in the final dataset
    df_merged = df_merged.rename(columns={
        'Disaster Type': 'disaster_type',
        'Country': 'country',
        'Region': 'region',
        'Event Name': 'event_name',
        'Location': 'emdat_location'
    })
    
    # Select and return only the necessary columns, ready for the next pipeline step
    df_selected = df_merged[[
        'DisNo.', 'disasterno', 'disaster_type', 'country', 'region', 'latitude', 'longitude', 
        'event_name', 'emdat_location',
        'geolocation', 'adm1', 'adm2', 'adm3', 'location',
        'start_date', 'date_minus_10', 'date_plus_10'
    ]].copy()

    #Remove any duplicate columns sharing the same name
    df_selected = df_selected.loc[:, ~df_selected.columns.duplicated()].copy()
    
    # Convert the start_date column to string for consistency in the final JSON
    df_selected['start_date'] = df_selected['start_date'].dt.strftime('%Y-%m-%d')

    df_selected = df_selected.rename(columns={'DisNo.': 'emdat_disaster_id'})

    def join_unique(values):
        cleaned = []
        for value in values:
            if pd.isna(value):
                continue
            text = str(value).strip()
            if text and text.lower() not in {'nan', 'none', 'null'} and text not in cleaned:
                cleaned.append(text)
        return ' | '.join(cleaned)

    # GDIS can contain multiple affected locations for the same EM-DAT event.
    # Aggregate them into one row per country-specific disaster while preserving
    # all location names as internal news-search context.
    df_selected = (
        df_selected
        .groupby('emdat_disaster_id', as_index=False, sort=False)
        .agg({
            'disasterno': 'first',
            'disaster_type': 'first',
            'country': 'first',
            'region': 'first',
            'latitude': 'first',
            'longitude': 'first',
            'event_name': join_unique,
            'emdat_location': join_unique,
            'geolocation': join_unique,
            'adm1': join_unique,
            'adm2': join_unique,
            'adm3': join_unique,
            'location': join_unique,
            'start_date': 'first',
            'date_minus_10': 'first',
            'date_plus_10': 'first',
        })
    )
    
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

def calculate_weather_summaries(daily_series):
    """
    Splits the 21-day daily weather series into pre-event (first 10 days)
    and post-event (last 10 days) periods, calculating statistical aggregates.
    """
    if not daily_series:
        return None, None

    # Slicing the arrays: Pre-event (indices 0-9), Post-event (indices 11-20)
    pre_rain = daily_series["rain_sum"][:10]
    post_rain = daily_series["rain_sum"][11:]
    
    pre_temp_max = daily_series["temperature_2m_max"][:10]
    post_temp_max = daily_series["temperature_2m_max"][11:]

    # Calculating aggregates
    pre_summary = {
        "total_rainfall_mm": round(sum(pre_rain), 2),
        "max_daily_rainfall_mm": max(pre_rain) if pre_rain else 0.0,
        "avg_max_temperature_c": round(sum(pre_temp_max) / len(pre_temp_max), 1) if pre_temp_max else None
    }

    post_summary = {
        "total_rainfall_mm": round(sum(post_rain), 2),
        "max_daily_rainfall_mm": max(post_rain) if post_rain else 0.0,
        "avg_max_temperature_c": round(sum(post_temp_max) / len(post_temp_max), 1) if post_temp_max else None
    }

    return pre_summary, post_summary
