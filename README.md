# EnvironmentCausalDataset

## Setup and Execution

This pipeline is designed to be fully automated, modular, and resilient. However, to respect data licensing and keep the repository lightweight, the raw source datasets are excluded from version control.

### Prerequisites & Data Placement

1. Clone this repository to your local machine.
2. Download the original source datasets from your official EMDAT and GDIS providers.
3. Run the pipeline once using the command below; this will automatically generate the required directory tree structure (including the `data/` and `results/` folders).
4. Place your raw CSV files inside the newly created `data/` directory, ensuring they match the expected file names configured in `support/constants.py`.

### Running the Pipeline

To execute the entire dataset compilation, weather data fetching, and causal aggregation loop, simply run the main script from the root directory:

```bash
python main.py