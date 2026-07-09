FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install standard linux tools needed for slurm querying if applicable
RUN apt-get update && apt-get install -y procps && rm -rf /var/lib/apt/lists/*

COPY . .

# Ensure load_pricelist (inside hpc-cost-comparator/) is importable
ENV PYTHONPATH=/app/hpc-cost-comparator

# Expose the port Uvicorn runs on
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]