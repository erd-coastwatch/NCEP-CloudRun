FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends netcdf-bin ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY config/requirements.txt config/requirements.txt
RUN pip install --no-cache-dir -r config/requirements.txt
COPY scripts/ scripts/
COPY templates/ncep_winds_slp.cdl templates/ncep_winds_slp.cdl
RUN ncgen -k netCDF-4 -o templates/ncep_gdas_template.nc templates/ncep_winds_slp.cdl
ENTRYPOINT ["python", "scripts/process.py"]
