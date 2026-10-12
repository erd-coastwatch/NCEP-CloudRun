"""Incremental NCEP annual files, using the existing ERDDAP schema."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import logging
import os
from pathlib import Path
import shutil
import time

import netCDF4
import numpy as np
import requests
import xarray as xr
from google.cloud import storage

LOG = logging.getLogger(__name__)
SOURCES = ['uwnd.sfc', 'vwnd.sfc', 'slp', 'air.sfc', 'pr_wtr', 'rhum.sfc', 'srfp', 'trpp']
NAMES = [s.split('.')[0] for s in SOURCES]
TEMPLATE = Path(__file__).resolve().parents[1] / 'templates/ncep_gdas_template.nc'


def retry(fn):
    for attempt in range(5):
        try:
            return fn()
        except Exception:
            if attempt == 4:
                raise
            LOG.warning('Source request failed; retry %d', attempt + 1, exc_info=True)
            time.sleep(2 ** attempt)


def open_source(prefix, year):
    base = os.getenv('SOURCE_URL', 'https://psl.noaa.gov/thredds/dodsC/Datasets/ncep/{variable}.{year}.nc')
    url = base.format(variable=prefix, year=year)
    # Preserve HTTPS, use the same Pydap backend as Dale's workflow.
    return retry(lambda: xr.open_dataset(url, engine='pydap', decode_times=False,
                                        mask_and_scale=False, timeout=120))


def expected_times(year, size, units, calendar='standard'):
    dates = [dt.datetime(year, 1, 1) + dt.timedelta(days=i) for i in range(size)]
    if dates and dates[-1].year != year:
        raise ValueError(f'{year}: source has more than one calendar year of daily records')
    return np.asarray(netCDF4.date2num(dates, units, calendar), dtype=float)


def normalized_times(values, attrs, year):
    raw = np.asarray(values, dtype=float).copy()
    units = attrs.get('units')
    if not units:
        raise ValueError('Source time units are missing')
    calendar = attrs.get('calendar', 'standard')
    expected = expected_times(year, len(raw), units, calendar)
    bad = ~np.isfinite(raw) | (np.abs(raw) > 1e9)
    if bad.any():
        LOG.warning('Repairing %d invalid times using the daily calendar for %d', bad.sum(), year)
        raw[bad] = expected[bad]
    # Never invent a timeline that disagrees with the remaining valid source times.
    if not np.allclose(raw, expected, rtol=0, atol=1e-6):
        raise ValueError(f'{year}: source is not a Jan 1 based daily timeline')
    decoded = netCDF4.num2date(raw, units, calendar)
    return np.asarray(netCDF4.date2num(decoded, 'days since 1800-1-1 00:00:0.0'), dtype=float)


def physical_values(raw, attrs):
    raw = np.asarray(raw)
    mask = ~np.isfinite(raw)
    for key in ('_FillValue', 'missing_value'):
        if key in attrs:
            for fill in np.asarray(attrs[key]).reshape(-1):
                mask |= raw == fill
    if np.issubdtype(raw.dtype, np.integer):
        mask |= raw == -99  # Dale's integer QC rule; floating point is preserved.
    values = raw.astype(np.float64) * attrs.get('scale_factor', 1.) + attrs.get('add_offset', 0.)
    return np.ma.array(values, mask=mask)


def write_values(target, raw, attrs, start):
    aliases = {'m/s': 'm s-1', 'degK': 'K', 'degrees Kelvin': 'K', 'mb': 'mbar',
               'kg/m^2': 'kg m-2', 'kg/m2': 'kg m-2', 'percent': '%'}
    source_units = str(attrs.get('units', '')).strip()
    target_units = str(target.getncattr('units')).strip()
    if aliases.get(source_units, source_units) != aliases.get(target_units, target_units):
        raise ValueError(f'{target.name}: source/target units differ; refusing implicit conversion')
    values = physical_values(raw, attrs)
    scale = float(getattr(target, 'scale_factor', 1.))
    offset = float(getattr(target, 'add_offset', 0.))
    packed = (values - offset) / scale
    if np.issubdtype(target.dtype, np.integer):
        packed = np.ma.round(packed)
        limits = np.iinfo(target.dtype)
        valid = packed.compressed()
        if valid.size and (valid.min() < limits.min or valid.max() > limits.max):
            raise ValueError(f'{target.name}: values exceed the target packing range')
    fill = getattr(target, '_FillValue', netCDF4.default_fillvals[target.dtype.str[1:]])
    packed = packed.filled(fill).astype(target.dtype)
    target.set_auto_maskandscale(False)
    target[start:start + len(raw)] = packed


def validate(path, year):
    with netCDF4.Dataset(path) as nc:
        times = np.asarray(nc['time'][:])
        expected = expected_times(year, len(times), nc['time'].units)
        if not len(times) or not np.allclose(times, expected, rtol=0, atol=1e-6):
            raise ValueError('Output time axis is empty or invalid')
        with netCDF4.Dataset(TEMPLATE) as template:
            for axis in ('latitude', 'longitude'):
                if not np.array_equal(nc[axis][:], template[axis][:]):
                    raise ValueError(f'Output {axis} differs from the ERDDAP grid')
            for name in NAMES:
                v, ref = nc[name], template[name]
                if v.dimensions != ref.dimensions or v.dtype != ref.dtype:
                    raise ValueError(f'{name}: schema mismatch')
                for key in ('units', 'scale_factor', 'add_offset', '_FillValue'):
                    if not np.array_equal(getattr(v, key, None), getattr(ref, key, None)):
                        raise ValueError(f'{name}: {key} differs from the template')


def update_year(path, year, chunk_size=10, refresh_days=2):
    sources = {}
    try:
        errors = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [(prefix, pool.submit(open_source, prefix, year)) for prefix in SOURCES]
            for prefix, future in futures:
                try:
                    sources[prefix.split('.')[0]] = future.result()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise RuntimeError('One or more required source files are unavailable') from errors[0]
        with netCDF4.Dataset(path, 'a') as out:
            if not out.dimensions['time'].isunlimited():
                raise ValueError('Annual baseline must have an unlimited time dimension')
            timelines = {}
            for name, source in sources.items():
                if name not in source:
                    raise ValueError(f'Source lacks {name}')
                timelines[name] = normalized_times(retry(lambda s=source: s.time.values),
                                                   source.time.attrs, year)
                for src_axis, dst_axis in [('lat', 'latitude'), ('lon', 'longitude')]:
                    axis = src_axis if src_axis in source else dst_axis
                    if not np.array_equal(retry(lambda s=source, a=axis: s[a].values), out[dst_axis][:]):
                        raise ValueError(f'{name}: source {axis} differs from the existing grid')
            lengths = {k: len(v) for k, v in timelines.items()}
            size = min(lengths.values())
            if len(set(lengths.values())) > 1:
                LOG.warning('Unequal source lengths: %s; using %d common records', lengths, size)
            for values in timelines.values():
                if not np.array_equal(values[:size], next(iter(timelines.values()))[:size]):
                    raise ValueError('Variables have different time coordinates')
            local_size = len(out.dimensions['time'])
            if size < local_size:
                raise ValueError(f'Source shorter than baseline ({size} < {local_size}); refusing truncation')
            if not size:
                raise ValueError('Source has no common records')
            if local_size and not np.array_equal(out['time'][:], next(iter(timelines.values()))[:local_size]):
                raise ValueError('Source timeline disagrees with existing annual file')
            start = max(0, local_size - refresh_days)
            changed = size > local_size or start < size
            for name, source in sources.items():
                var = source[name]
                spatial = tuple(x for x in var.dims if x != 'time')
                if len(spatial) != 2:
                    raise ValueError(f'{name}: expected two spatial dimensions, got {var.dims}')
                for i in range(start, size, chunk_size):
                    j = min(size, i + chunk_size)
                    LOG.info('%d %s records [%d:%d]', year, name, i, j)
                    raw = retry(lambda v=var, a=i, b=j: v.isel(time=slice(a, b)).transpose('time', *spatial).values)
                    if raw.shape[1:] != (73, 144):
                        raise ValueError(f'{name}: unexpected data grid shape {raw.shape}')
                    write_values(out[name], raw, var.attrs, i)
                # The template's historical actual_range is intentionally omitted.
                if 'actual_range' in out[name].ncattrs():
                    out[name].delncattr('actual_range')
            out['time'][:] = next(iter(timelines.values()))[:size]
            out['time'].actual_range = np.array([out['time'][0], out['time'][-1]])
            out.time_coverage_start = f'{year}-01-01T00:00:00Z'
            out.time_coverage_end = netCDF4.num2date(out['time'][-1], out['time'].units).strftime('%Y-%m-%dT00:00:00Z')
        validate(path, year)
        return changed
    finally:
        for source in sources.values():
            source.close()


def flag_erddap():
    if os.getenv('JOB_MODE', 'staging') == 'staging':
        LOG.info('Staging: production ERDDAP flagging disabled')
        return
    base = os.getenv('ERDDAP_BASE_URL', '').rstrip('/')
    key_file = os.getenv('ERDDAP_FLAG_JSON', '/secrets/erddap_flags/file')
    secret_name = os.getenv('ERDDAP_FLAG_SECRET', '')
    if not base:
        LOG.info('ERDDAP flagging disabled; normal ERDDAP update/reload applies')
        return
    if secret_name:
        from google.cloud import secretmanager
        if '/locations/' in secret_name:
            region = secret_name.split('/locations/', 1)[1].split('/', 1)[0]
            client = secretmanager.SecretManagerServiceClient(
                client_options={'api_endpoint': f'secretmanager.{region}.rep.googleapis.com'})
        else:
            client = secretmanager.SecretManagerServiceClient()
        result = client.access_secret_version(request={'name': secret_name}, timeout=60)
        keys = json.loads(result.payload.data.decode('utf-8'))
    else:
        keys = json.loads(Path(key_file).read_text())
    key = keys['ncep_gdas_global']
    if not isinstance(key, str) or not key:
        raise ValueError('Missing ncep_gdas_global flag key')
    # Never log the request URL: it contains the secret key.
    try:
        response = requests.get(base + '/setDatasetFlag.txt',
                                params={'datasetID': 'ncep_gdas_global', 'flagKey': key},
                                timeout=60, verify=os.getenv('ERDDAP_INSECURE', 'false').lower() != 'true')
        if response.status_code != 200:
            raise RuntimeError(f'ERDDAP flag returned HTTP {response.status_code}')
        if 'success' not in response.text.lower():
            raise RuntimeError('ERDDAP did not confirm successful flagging')
    except requests.RequestException:
        raise RuntimeError('ERDDAP flag request failed') from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-year', type=int)
    parser.add_argument('--end-year', type=int)
    parser.add_argument('--refresh-days', type=int, default=2)
    parser.add_argument('--chunk-size', type=int, default=10)
    parser.add_argument('--dry-run', action='store_true', help='Download/build/validate without GCS writes or flags')
    args = parser.parse_args()
    if (args.start_year is None) != (args.end_year is None):
        parser.error('Provide both --start-year and --end-year')
    if args.chunk_size < 1 or args.refresh_days < 0:
        parser.error('chunk-size must be positive and refresh-days nonnegative')
    current = dt.datetime.now(dt.timezone.utc).year
    years = [current, current - 1] if args.start_year is None else list(range(args.start_year, args.end_year + 1))
    if not years:
        parser.error('The year range is empty')
    job_mode = os.getenv('JOB_MODE', 'staging')
    if job_mode not in ('staging', 'production'):
        parser.error('JOB_MODE must be staging or production')
    bucket = storage.Client().bucket(os.getenv('BUCKET', os.getenv('OUTPUT_BUCKET', 'erddap-swfsc-erd')))
    default_prefix = '_staging/ncep-gdas' if job_mode == 'staging' else 'satellite/ncep_gdas'
    prefix = os.getenv('PREFIX', os.getenv('OUTPUT_PREFIX', default_prefix)).strip('/')
    seed_prefix = os.getenv('SEED_PREFIX', 'satellite/ncep_gdas').strip('/')
    if job_mode == 'staging' and not prefix.startswith('_staging/'):
        parser.error('Staging output PREFIX must be under _staging/')
    work = Path(os.getenv('WORK_DIR', '/tmp/ncep-gdas'))
    work.mkdir(parents=True, exist_ok=True)
    failures = []
    for year in years:
        try:
            filename = f'ncep_gdas_surface_{year}.nc'
            path = work / filename
            blob = bucket.blob(f'{prefix}/{filename}')
            if blob.exists():
                blob.reload()
                generation = int(blob.generation)
                blob.download_to_filename(str(path), if_generation_match=generation)
                validate(path, year)
            else:
                generation = 0
                seed = bucket.blob(f'{seed_prefix}/{filename}')
                if seed_prefix != prefix and seed.exists():
                    seed.reload()
                    seed.download_to_filename(str(path), if_generation_match=int(seed.generation))
                    validate(path, year)
                    LOG.info('Seeded %d from production; writes target %s', year, prefix)
                else:
                    shutil.copyfile(TEMPLATE, path)
            changed = update_year(path, year, args.chunk_size, args.refresh_days)
            if changed and not args.dry_run:
                # Atomic object replacement; fail rather than overwrite a concurrent execution.
                blob.upload_from_filename(str(path), if_generation_match=generation,
                                          content_type='application/x-netcdf', timeout=300)
                LOG.info('Published gs://%s/%s', bucket.name, blob.name)
            elif args.dry_run:
                LOG.info('Dry run validated %s; no publication', path)
        except Exception:
            LOG.exception('Year %d failed', year)
            failures.append(year)
    # Reissue even after a no-change run, so a prior post-upload flag failure can recover.
    if not args.dry_run:
        flag_erddap()
    if failures:
        raise RuntimeError(f'Failed years: {failures}')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    main()
