# dags/iis_etl.py
import os
import re
import shutil
import gzip
from datetime import datetime, timedelta
from glob import glob
import logging
import time
import pandas as pd
import numpy as np
from user_agents import parse
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.models import Variable
from airflow.utils.task_group import TaskGroup
from airflow.operators.empty import EmptyOperator
from airflow.exceptions import AirflowSkipException
from sqlalchemy.exc import SQLAlchemyError
from airflow.models import DagModel
from airflow.utils.session import provide_session
from pathlib import Path
import urllib.request
import json
from functools import lru_cache
import geoip2.database
from geoip2 import database
from typing import Optional, Tuple


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Constants
DEFAULT_CHUNKSIZE = 5000
MAX_CONCURRENT_PROCESSING = 4
LOG_RETENTION_DAYS = 30
GEOIP_DB_PATH = Path('/opt/airflow/data/GeoLite2-City.mmdb')  # Updated path

def get_db_engine():
    """Get SQLAlchemy engine with proper ETL credentials"""
    try:
        pg_hook = PostgresHook(postgres_conn_id='postgres_iis')
        engine = pg_hook.get_sqlalchemy_engine()
        
        # Test connection immediately
        with engine.connect() as conn:
            if not conn.execute("SELECT 1").scalar() == 1:
                raise ValueError("Connection test failed")
        return engine
        
    except Exception as e:
        logger.error(f"Database connection failed: {str(e)}")
        raise

def check_and_pause_dag(**context):
    """
    Check for log files and pause DAG if none found
    Add this as your first task
    """
    raw_dir = context['params'].get('raw_log_dir', '/opt/airflow/data/raw_logs')
    min_files = context['params'].get('min_files', 1)  # Minimum files needed
    
    if not any(Path(raw_dir).glob('*.log')) or len(list(Path(raw_dir).glob('*.log'))) < min_files:
        dag_id = context['dag'].dag_id
        logger.warning(f"Pausing DAG {dag_id} - no log files found")
        
        @provide_session
        def pause_dag(session=None):
            dag_model = session.query(DagModel).filter(DagModel.dag_id == dag_id).first()
            if dag_model:
                dag_model.is_paused = True
                session.commit()
        
        pause_dag()
        raise AirflowSkipException("Paused DAG - no files to process")
    
    return True

def verify_postgres_connection():
    """Verify PostgreSQL connection exists and is working"""
    try:
        hook = PostgresHook(postgres_conn_id='postgres_iis')
        conn = hook.get_conn()
        conn.close()
        logger.info("PostgreSQL connection verified successfully")
        return True
    except Exception as e:
        logger.error(f"PostgreSQL connection failed: {str(e)}")
        raise

def redact_sensitive_data(text: str) -> str:
    """Redact PII from log entries with improved patterns"""
    if pd.isna(text) or text == '-':
        return text
        
    patterns = [
        (r'(auth|pass|token|key|secret|pwd)=([^&\s]*)', r'\1=REDACTED'),
        (r'email=([^&\s]*)', 'email=REDACTED'),
        (r'cc=(\d{4})\d{4}(\d{4})', r'cc=\1XXXX\2'),
        (r'phone=([^&\s]*)', 'phone=REDACTED'),
        (r'name=([^&\s]*)', 'name=REDACTED')
    ]
    
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, str(text), flags=re.IGNORECASE)
    return text

def transform_log_data(df: pd.DataFrame) -> pd.DataFrame:
    """Enhanced transformation with robust type handling"""
    df = df.copy()
    
    # Ensure IP columns remain as strings
    for ip_col in ['s-ip', 'c-ip']:
        df[ip_col] = df[ip_col].astype(str)
    
    # Robust timestamp handling
    try:
        df['date'] = df['date'].astype(str).str.strip()
        df['time'] = df['time'].astype(str).str.strip().str.split('.').str[0]
        df['timestamp'] = pd.to_datetime(
            df['date'] + ' ' + df['time'],
            format='%Y-%m-%d %H:%M:%S',
            errors='coerce'
        ).dt.tz_localize('UTC')
        
        if df['timestamp'].isna().any():
            bad_count = df['timestamp'].isna().sum()
            logger.warning(f"Found {bad_count} invalid timestamps")
            df = df[~df['timestamp'].isna()].copy()
    except Exception as e:
        logger.error(f"Timestamp creation failed: {str(e)}")
        raise
    
    # Time features (only those that match dim_time columns)
    df['hour'] = df['timestamp'].dt.hour
    df['date'] = df['timestamp'].dt.date
    df['month'] = df['timestamp'].dt.month
    df['quarter'] = df['timestamp'].dt.quarter
    df['year'] = df['timestamp'].dt.year
    df['time_period'] = np.select(
        [
            df['hour'].between(0, 6, inclusive='left'),
            df['hour'].between(6, 12, inclusive='left'),
            df['hour'].between(12, 18, inclusive='left'),
            df['hour'].between(18, 24, inclusive='left')
        ],
        ['Night', 'Morning', 'Afternoon', 'Evening'],
        default='Night'
    )
    
    # 4. Client features with null safety
    df['ip_prefix'] = df['c-ip'].str.extract(r'^(\d+\.\d+)')
    df['user_agent'] = df['cs(User-Agent)'].apply(
        lambda ua: str(parse(ua)) if pd.notna(ua) else None
    )
    
    # 5. Security flags with enhanced patterns
    df['is_error'] = df['sc-status'] >= 400
    df['is_suspicious'] = (
        (df['sc-status'] == 404) & 
        df['cs-uri-stem'].str.contains(
            r'wp-admin|phpmyadmin|\.env|config|\.git|backup', 
            case=False, 
            na=False
        )
    )
    df['is_bot'] = df['cs(User-Agent)'].str.contains(
        r'bot|crawl|slurp|spider|archiver|facebook|twitter|googlebot', 
        case=False, 
        na=False
    )
    
    # 6. URL analysis with null checks
    df['uri_depth'] = df['cs-uri-stem'].str.count('/').fillna(0) - 1
    df['is_api_call'] = df['cs-uri-stem'].str.contains(
        r'/api/|/v[0-9]/|/graphql|/rest', 
        case=False, 
        na=False
    )
    
    # 7. Validate output
    required_columns = ['timestamp', 'c-ip', 'sc-status', 'is_error', 'is_bot']
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in output: {missing}")
    
    return df

def get_next_log_file() -> Optional[str]:
    """Get oldest unprocessed log file with improved filtering"""
    raw_dir = Variable.get("raw_log_dir", "/opt/airflow/data/raw_logs")
    files = sorted(
        Path(raw_dir).glob('u_ex*.log'),
        key=lambda f: f.stat().st_mtime
    )
    return str(files[0]) if files else None

def parse_user_agent(ua_string: Optional[str]) -> dict:
    """Enhanced user agent parsing with additional fields"""
    if not ua_string or pd.isna(ua_string):
        return {
            'browser': None,
            'os': None,
            'device_type': None,
            'is_bot': False,
            'is_mobile': False
        }
    
    ua = parse(ua_string)
    return {
        'browser': ua.browser.family,
        'os': ua.os.family,
        'device_type': 'Mobile' if ua.is_mobile else ('Tablet' if ua.is_tablet else 'Desktop'),
        'is_bot': ua.is_bot,
        'is_mobile': ua.is_mobile
    }

def extract_domain(url: Optional[str]) -> Optional[str]:
    """Improved domain extraction from referer"""
    if not url or pd.isna(url) or url == '-':
        return None
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        return domain.split(':')[0].lower()  # Remove port and normalize case
    except Exception:
        return None
    
def parse_iis_line_with_recovery(line: str, line_num: int) -> Optional[dict]:
    """Enhanced flexible line parser with better recovery logic"""
    try:
        parts = line.strip().split()
        
        # Handle field count variations with more intelligent recovery
        if len(parts) < 14:
            logger.warning(f"Line {line_num} has only {len(parts)} fields - attempting recovery")
            
            # Special case: missing time-taken at end
            if len(parts) == 13:
                parts.append('0')  # Default time-taken
            # Special case: missing cs-username
            elif len(parts) == 12 and parts[7].startswith(('HTTP', 'Mozilla')):
                parts.insert(7, '-')  # Insert missing username
                parts.append('0')    # Add time-taken
            else:
                # Generic padding for other cases
                parts += ['-'] * (14 - len(parts))
                
        elif len(parts) > 14:
            logger.warning(f"Line {line_num} has {len(parts)} fields - attempting smart truncation")
            
            # Special case: extra fields after time-taken (common in some IIS versions)
            if parts[13].isdigit():
                parts = parts[:14]  # Keep first 14 fields
            else:
                # Look for the first numeric field after position 13 as time-taken
                for i in range(13, len(parts)):
                    if parts[i].isdigit():
                        parts = parts[:13] + [parts[i]]  # Keep first 13 + time-taken
                        break
        
        # Validate and parse datetime
        datetime_str = f"{parts[0]} {parts[1]}"
        try:
            pd.to_datetime(datetime_str)  # Validate
        except ValueError:
            # Attempt to fix common datetime format issues
            if '.' in parts[1]:  # IIS sometimes adds milliseconds
                parts[1] = parts[1].split('.')[0]
                datetime_str = f"{parts[0]} {parts[1]}"
                pd.to_datetime(datetime_str)  # Retry
            
        return {
            'date': parts[0],
            'time': parts[1],
            'datetime_str': datetime_str,
            's-ip': parts[2],
            'cs-method': parts[3],
            'cs-uri-stem': parts[4],
            'cs-uri-query': redact_sensitive_data(parts[5]),
            's-port': safe_int(parts[6]),
            'cs-username': parts[7],
            'c-ip': parts[8],
            'cs(User-Agent)': parts[9],
            'sc-status': safe_int(parts[10], default=0),
            'sc-substatus': safe_int(parts[11]),
            'sc-win32-status': safe_int(parts[12]),
            'time-taken': safe_int(parts[13])
        }
        
    except Exception as e:
        logger.warning(f"Skipping line {line_num} due to error: {str(e)}")
        return None

def safe_int(value: str, default: Optional[int] = None) -> Optional[int]:
    """Improved safe integer conversion with logging"""
    if value in ('-', '', None):
        return default
    try:
        return int(value)
    except ValueError:
        logger.debug(f"Could not convert '{value}' to integer, using default {default}")
        return default
    
def validate_iis_record(record: list) -> bool:
    """Enhanced record validation"""
    if len(record) < 14:
        return False
    try:
        # Validate required numeric fields
        int(record[10])  # sc-status
        int(record[13])  # time-taken
        
        # Validate datetime
        datetime_str = f"{record[0]} {record[1]}"
        pd.to_datetime(datetime_str)
        return True
    except (ValueError, IndexError):
        return False

def log_file_healthcheck(file_path: str) -> bool:
    """Enhanced log file validation"""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            for _ in range(10):  # Check first 10 non-comment lines
                line = next(f)
                while line.startswith('#'):
                    line = next(f)
                if validate_iis_record(line.strip().split()):
                    return True
        return False
    except Exception:
        return False

def is_valid_iis_log(file_path):
    """Check if file has basic IIS log structure"""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            # Check first 10 non-comment lines
            valid_lines = 0
            for _ in range(10):
                line = f.readline()
                while line and line.startswith('#'):
                    line = f.readline()
                if line and len(line.strip().split()) >= 14:  # IIS has 14+ fields
                    valid_lines += 1
                if valid_lines >= 3:  # Need at least 3 valid lines
                    return True
        return False
    except Exception as e:
        logger.debug(f"Validation failed for {file_path}: {str(e)}")
        return False

@lru_cache(maxsize=128)
def get_ip_location(ip: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Cached geolocation lookup with improved error handling"""
    if not ip or ip.startswith(('10.', '172.', '192.168.', '127.')):
        return (None, None, None, None)
    
    try:
        if GEOIP_DB_PATH.exists():
            with database.Reader(str(GEOIP_DB_PATH)) as reader:
                response = reader.city(ip)
                return (
                    response.country.name if response.country else None,
                    response.subdivisions.most_specific.name if response.subdivisions else None,
                    response.city.name if response.city else None,
                    response.postal.code if response.postal else None
                )
        else:
            # Fallback to API if local DB not available
            with urllib.request.urlopen(f"http://ip-api.com/json/{ip}?fields=country,regionName,city,zip") as response:
                data = json.loads(response.read().decode())
                return (
                    data.get('country'),
                    data.get('regionName'),
                    data.get('city'),
                    data.get('zip')
                )
    except Exception as e:
        logger.debug(f"Geolocation lookup failed for IP {ip}: {str(e)}")
        return (None, None, None, None)

def load_dimensions(**kwargs):
    """Enhanced dimension loading with better error handling and performance"""
    ti = kwargs.get('ti')
    
    try:
        # Get transformed data
        transformed_data = ti.xcom_pull(task_ids='processing.transform', key='transformed_data')
        if not transformed_data:
            raise ValueError("No data received from transform task")
        
        df = pd.read_json(transformed_data)
        
        if len(df) == 0:
            raise AirflowSkipException("No data to load")
        
        engine = get_db_engine()
        
        # ========== 1. TIME DIMENSION ==========
        with engine.connect() as conn:
            # Verify connection is alive
            conn.execute("SELECT 1")
            
            # Check which columns exist in dim_time
            existing_columns = [
                col[0] for col in 
                conn.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = 'dim_time'
                """).fetchall()
            ]
            
            # Prepare time data with only available columns
            time_data = {
                'timestamp': df['timestamp'],
                'hour': df['timestamp'].dt.hour,
                'date': df['timestamp'].dt.date,
                'month': df['timestamp'].dt.month,
                'quarter': df['timestamp'].dt.quarter,
                'year': df['timestamp'].dt.year
            }
            
            # Add optional columns if they exist in the table
            if 'time_period' in existing_columns:
                time_data['time_period'] = df.get('time_period', 'Unknown')
            if 'is_weekend' in existing_columns:
                time_data['is_weekend'] = df['timestamp'].dt.dayofweek >= 5
            if 'day_of_week' in existing_columns:
                time_data['day_of_week'] = df['timestamp'].dt.day_name()
            
            time_df = pd.DataFrame(time_data).drop_duplicates('timestamp')
            
            # Create temp table
            time_df.to_sql('temp_time', conn, if_exists='replace', index=False)
            
            # Build dynamic column list for insert
            columns = [col for col in time_df.columns if col in existing_columns]
            columns_str = ', '.join(columns)
            select_str = ', '.join(columns)
            
            # Execute insert
            conn.execute(f"""
                INSERT INTO dim_time ({columns_str})
                SELECT {select_str}
                FROM temp_time
                ON CONFLICT (timestamp) DO NOTHING
            """)
            
            # Clean up
            conn.execute("DROP TABLE IF EXISTS temp_time")
            conn.close()  # Explicitly close connection

        # ========== 2. CLIENT DIMENSION ==========
        with engine.connect() as conn:
            conn.execute("SELECT 1")  # Verify connection
            # Prepare enhanced client data with geolocation
            client_df = df[['c-ip', 'cs(User-Agent)', 'is_bot']].drop_duplicates('c-ip')
            
            # Parse user agent with enhanced fields
            ua_parsed = client_df['cs(User-Agent)'].apply(
                lambda ua: parse(ua) if pd.notna(ua) else None
            )
            
            # Get geolocation data in batch
            locations = client_df['c-ip'].apply(lambda ip: get_ip_location(ip))
            client_df['country'] = locations.apply(lambda x: x[0])
            client_df['state'] = locations.apply(lambda x: x[1])
            client_df['city'] = locations.apply(lambda x: x[2])
            client_df['zip_code'] = locations.apply(lambda x: x[3])
            
            # Create temp table with optimized schema
            client_data = {
                'ip_address': client_df['c-ip'],
                'user_agent': client_df['cs(User-Agent)'],
                'browser': ua_parsed.apply(lambda x: x.browser.family if x else None),
                'os': ua_parsed.apply(lambda x: x.os.family if x else None),
                'device_type': ua_parsed.apply(
                    lambda x: 'Mobile' if x and x.is_mobile else (
                        'Tablet' if x and x.is_tablet else 'Desktop'
                    )
                ),
                'country': client_df['country'],
                'state': client_df['state'],
                'city': client_df['city'],
                'zip_code': client_df['zip_code']
                # 'ip_prefix': client_df['c-ip'].str.extract(r'^(\d+\.\d+)')[0]
            }
            pd.DataFrame(client_data).to_sql('temp_clients', conn, if_exists='replace', index=False)
            
            # Bulk upsert
            conn.execute("""
                INSERT INTO dim_clients (
                    ip_address, user_agent, browser, os, device_type,
                    country, state, city, zip_code
                )
                SELECT 
                    ip_address, user_agent, browser, os, device_type,
                    country, state, city, zip_code
                FROM temp_clients
                ON CONFLICT (ip_address) DO UPDATE SET
                    user_agent = EXCLUDED.user_agent,
                    browser = EXCLUDED.browser,
                    os = EXCLUDED.os,
                    device_type = EXCLUDED.device_type,
                    country = EXCLUDED.country,
                    state = EXCLUDED.state,
                    city = EXCLUDED.city,
                    zip_code = EXCLUDED.zip_code
            """)
            conn.execute("DROP TABLE temp_clients")

        # ========== 3. REQUEST DIMENSION ==========
        with engine.begin() as conn:
            # Prepare enhanced request data
            request_df = df[['cs-method', 'cs-uri-stem', 'cs-uri-query', 'cs-username']]\
                .drop_duplicates(['cs-method', 'cs-uri-stem'])\
                .rename(columns={
                    'cs-method': 'method',
                    'cs-uri-stem': 'uri_stem',
                    'cs-uri-query': 'uri_query',
                    'cs-username': 'username'
                })
            request_df.to_sql('temp_requests', conn, if_exists='replace', index=False)
            
            # Bulk upsert
            conn.execute("""
                INSERT INTO dim_requests 
                (method, uri_stem, uri_query, username)
                SELECT method, uri_stem, uri_query, username
                FROM temp_requests
                ON CONFLICT (method, uri_stem) DO UPDATE SET
                    uri_query = EXCLUDED.uri_query,
                    username = EXCLUDED.username
            """)
            conn.execute("DROP TABLE temp_requests")

        # ========== 4. RESPONSE DIMENSION ==========
        with engine.begin() as conn:
            # Prepare response data with error classification
            response_df = df[['sc-status', 'sc-substatus', 'sc-win32-status']]\
                .drop_duplicates()\
                .rename(columns={
                    'sc-status': 'status_code',
                    'sc-substatus': 'substatus',
                    'sc-win32-status': 'win32_status'
                })
            response_df.to_sql('temp_responses', conn, if_exists='replace', index=False)
            
            # Bulk insert
            conn.execute("""
                INSERT INTO dim_responses 
                (status_code, substatus, win32_status)
                SELECT status_code, substatus, win32_status
                FROM temp_responses
                ON CONFLICT (status_code, substatus, win32_status) DO NOTHING
            """)
            conn.execute("DROP TABLE temp_responses")

        conn.close()

        logger.info("Successfully updated all dimension relationships")
        return "Dimension loading completed successfully"
        
    except Exception as e:
        logger.error(f"Dimension loading failed: {str(e)}", exc_info=True)
        try:
            if 'conn' in locals():
                conn.close()
        except:
            pass
        raise

def extract(**kwargs):
    """IIS log parser that moves invalid files to invalid_logs folder"""
    ti = kwargs.get('ti')
    processing_path = None
    
    try:
        # Get the next log file to process
        log_path = get_next_log_file()
        if not log_path:
            logger.info("No logs found to process")
            raise AirflowSkipException("No logs to process")

        # Set up paths
        invalid_logs_dir = os.path.join(os.path.dirname(log_path), 'invalid_logs')
        os.makedirs(invalid_logs_dir, exist_ok=True)
        processing_dir = Variable.get("processing_dir", "/opt/airflow/data/processing")
        os.makedirs(processing_dir, exist_ok=True)
        processing_path = os.path.join(processing_dir, os.path.basename(log_path))

        # Validate the log file
        if not is_valid_iis_log(log_path):
            logger.warning(f"Invalid log file detected: {log_path}")
            
            # Move to invalid_logs directory with timestamp
            invalid_path = os.path.join(
                invalid_logs_dir,
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.path.basename(log_path)}"
            )
            shutil.move(log_path, invalid_path)
            logger.info(f"Moved invalid log to: {invalid_path}")
            raise AirflowSkipException(f"Invalid log file moved to invalid_logs")

        # Process valid log file
        shutil.move(log_path, processing_path)
        ti.xcom_push(key='processing_path', value=processing_path)

        records = []
        line_stats = {
            'total': 0,
            'comments': 0,
            'empty': 0,
            'malformed': 0,
            'valid': 0,
            'recovered': 0
        }

        with open(processing_path, 'r', encoding='utf-8', errors='replace') as f:
            for line_num, line in enumerate(f, 1):
                line_stats['total'] += 1
                
                # Skip comments and empty lines
                if line.startswith('#'):
                    line_stats['comments'] += 1
                    continue
                if not line.strip():
                    line_stats['empty'] += 1
                    continue
                
                # Parse with flexible field handling
                record = parse_iis_line_with_recovery(line, line_num)
                if not record:
                    line_stats['malformed'] += 1
                    continue
                
                # Track recovered records
                if len(line.strip().split()) != 14:
                    line_stats['recovered'] += 1
                
                records.append(record)
                line_stats['valid'] += 1

        # Log detailed statistics
        logger.info(
            f"Line statistics - Total: {line_stats['total']}, "
            f"Valid: {line_stats['valid']} ({line_stats['recovered']} recovered), "
            f"Comments: {line_stats['comments']}, "
            f"Empty: {line_stats['empty']}, "
            f"Malformed: {line_stats['malformed']}"
        )

        if not records:
            logger.error(f"No valid records found in {processing_path}")
            raise AirflowSkipException("No valid records found in log file")

        # Convert to DataFrame with memory optimization
        df = pd.DataFrame(records)
        logger.info(f"Successfully parsed {len(df)} records from {processing_path}")
        
        # Return as JSON with ISO date format and compression
        json_data = df.to_json(orient='records', date_format='iso', compression='gzip')
        ti.xcom_push(key='extracted_data', value=json_data)
        return json_data

    except Exception as e:
        logger.error(f"Extraction failed: {str(e)}", exc_info=True)
        if processing_path and os.path.exists(processing_path):
            # Move file back to original location on failure
            try:
                shutil.move(processing_path, log_path)
            except Exception as move_error:
                logger.error(f"Failed to return file to original location: {str(move_error)}")
        raise

def transform(**kwargs):
    """Enhanced transformation with additional features and validation"""
    ti = kwargs.get('ti')
    
    # Get data from extract task with compression support
    extracted_data = ti.xcom_pull(task_ids='processing.extract', key='extracted_data')
    if not extracted_data:
        raise ValueError("No data received from extract task")

    try:
        # Load with explicit schema and memory optimization
        df = pd.read_json(extracted_data, orient='records', compression='gzip')
        
        # Apply enhanced transformation
        df = transform_log_data(df)
        
        # Additional validation
        required_columns = ['timestamp', 'c-ip', 'sc-status', 'is_error', 'is_bot']
        if not all(col in df.columns for col in required_columns):
            missing = set(required_columns) - set(df.columns)
            raise ValueError(f"Missing required columns in transformed data: {missing}")
        
        # Return compressed JSON to reduce XCom size
        json_data = df.to_json(orient='records', date_format='iso', compression='gzip')
        ti.xcom_push(key='transformed_data', value=json_data)
        logger.info(f"Successfully transformed {len(df)} records")
        return json_data
        
    except Exception as e:
        logger.error(f"Transform failed: {str(e)}", exc_info=True)
        raise

def load(**kwargs):
    """Enhanced load with better performance and error handling"""
    ti = kwargs.get('ti')
    
    try:
        # Get transformed data with compression support
        transformed_data = ti.xcom_pull(task_ids='processing.transform', key='transformed_data')
        if not transformed_data:
            raise ValueError("No data received from transform task")
        
        df = pd.read_json(transformed_data, orient='records', compression='gzip')
        
        if len(df) == 0:
            raise AirflowSkipException("No data to load")
        
        # Get database connection
        engine = get_db_engine()
        
        # Prepare column mapping with additional fields
        column_mapping = {
            'timestamp': 'timestamp',
            'date': 'date',
            'time': 'time',
            's-ip': 's_ip',
            'cs-method': 'cs_method',
            'cs-uri-stem': 'cs_uri_stem',
            'cs-uri-query': 'cs_uri_query',
            's-port': 's_port',
            'cs-username': 'cs_username',
            'c-ip': 'c_ip',
            'cs(User-Agent)': 'cs_user_agent',
            'sc-status': 'sc_status',
            'sc-substatus': 'sc_substatus',
            'sc-win32-status': 'sc_win32_status',
            'time-taken': 'time_taken',
            'hour': 'hour',
            'time_period': 'time_period',
            'day_of_week': 'day_of_week',
            'ip_prefix': 'ip_prefix',
            'user_agent': 'user_agent',
            'is_error': 'is_error',
            'is_suspicious': 'is_suspicious',
            'is_bot': 'is_bot',
            'uri_depth': 'uri_depth',
            'is_api_call': 'is_api_call'
        }
        
        # Rename columns to match database schema
        df = df.rename(columns={k: v for k, v in column_mapping.items() if k in df.columns})
        
        # Load data with transaction management
        with engine.begin() as conn:
            # Get existing columns
            existing_columns = [col['name'] for col in conn.execute("""
                SELECT column_name AS name 
                FROM information_schema.columns 
                WHERE table_name = 'fact_visits'
            """).fetchall()]
            
            # Filter to only existing columns
            df = df[[col for col in df.columns if col in existing_columns]]
            
            # Batch load with progress logging
            chunksize = 5000
            total_rows = len(df)
            loaded_rows = 0
            
            for i in range(0, total_rows, chunksize):
                chunk = df.iloc[i:i + chunksize]
                chunk.to_sql(
                    'fact_visits',
                    conn,
                    if_exists='append',
                    index=False,
                    method='multi'
                )
                loaded_rows += len(chunk)
                logger.info(f"Loaded {loaded_rows}/{total_rows} rows ({loaded_rows/total_rows:.1%})")
            
            # Log final load stats
            logger.info(f"Successfully loaded {loaded_rows} fact records")
            
            # Enhanced FK updates with batch processing
            update_counts = {}
            
            # 1. Link to dim_time (batch update)
            time_update = conn.execute("""
                WITH updated AS (
                    UPDATE fact_visits f
                    SET time_id = t.time_id
                    FROM dim_time t
                    WHERE f.timestamp = t.timestamp
                    AND f.time_id IS NULL
                    RETURNING 1
                )
                SELECT COUNT(*) FROM updated
            """).scalar()
            update_counts['time'] = time_update
            
            # 2. Link to dim_clients (batch update)
            client_update = conn.execute("""
                WITH updated AS (
                    UPDATE fact_visits f
                    SET client_id = c.client_id
                    FROM dim_clients c
                    WHERE f.c_ip = c.ip_address
                    AND f.client_id IS NULL
                    RETURNING 1
                )
                SELECT COUNT(*) FROM updated
            """).scalar()
            update_counts['clients'] = client_update
            
            # 3. Link to dim_requests (conditional batch update)
            if all(col in df.columns for col in ['cs_method', 'cs_uri_stem']):
                request_update = conn.execute("""
                    WITH updated AS (
                        UPDATE fact_visits f
                        SET request_id = r.request_id
                        FROM dim_requests r
                        WHERE f.cs_method = r.method
                        AND f.cs_uri_stem = r.uri_stem
                        AND f.request_id IS NULL
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM updated
                """).scalar()
                update_counts['requests'] = request_update
            
            # 4. Link to dim_responses (batch update)
            response_update = conn.execute("""
                WITH updated AS (
                    UPDATE fact_visits f
                    SET response_id = r.response_id
                    FROM dim_responses r
                    WHERE f.sc_status = r.status_code
                    AND (f.sc_substatus IS NULL OR f.sc_substatus = r.substatus)
                    AND (f.sc_win32_status IS NULL OR f.sc_win32_status = r.win32_status)
                    AND f.response_id IS NULL
                    RETURNING 1
                )
                SELECT COUNT(*) FROM updated
            """).scalar()
            update_counts['responses'] = response_update
            
            logger.info(
                f"Updated foreign keys: "
                f"time({update_counts['time']}), "
                f"clients({update_counts['clients']}), "
                f"requests({update_counts.get('requests', 'N/A')}), "
                f"responses({update_counts['responses']})"
            )
            
        return (
            f"Loaded {loaded_rows} records. "
            f"Dimension links: {update_counts}"
        )
        
    except Exception as e:
        logger.error(f"Load failed: {str(e)}", exc_info=True)
        raise

def archive(**kwargs):
    """Enhanced archive with better file handling"""
    ti = kwargs['ti']
    try:
        file_path = ti.xcom_pull(task_ids='processing.extract', key='processing_path')
        if not file_path:
            logger.info("No file path found in XCom for archiving")
            return
        
        if not os.path.exists(file_path):
            logger.warning(f"File {file_path} does not exist for archiving")
            return
        
        archive_dir = Variable.get("archive_dir", "/opt/airflow/data/archive")
        os.makedirs(archive_dir, exist_ok=True)
        
        # Create timestamped archive path
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        archive_file = f"{archive_dir}/{timestamp}_{os.path.basename(file_path)}.gz"
        
        logger.info(f"Archiving {file_path} to {archive_file}")
        
        # Atomic archive operation
        temp_file = f"{archive_file}.tmp"
        with open(file_path, 'rb') as f_in:
            with gzip.open(temp_file, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        # Rename temp to final
        os.rename(temp_file, archive_file)
        
        # Verify archive
        if os.path.getsize(archive_file) == 0:
            raise ValueError("Created empty archive file")
        
        # Remove original
        os.remove(file_path)
        
        # Clean old files with size limit
        max_archive_size = 10 * 1024 * 1024 * 1024  # 10GB
        cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
        
        archive_files = sorted(Path(archive_dir).glob('*.gz'), key=os.path.getctime)
        total_size = sum(f.stat().st_size for f in archive_files)
        
        for f in archive_files:
            if (datetime.fromtimestamp(f.stat().st_ctime) < cutoff) or (total_size > max_archive_size):
                logger.info(f"Removing old archive file {f} (size: {f.stat().st_size/1024/1024:.2f}MB)")
                os.remove(f)
                total_size -= f.stat().st_size
                
    except Exception as e:
        logger.error(f"Archive failed: {str(e)}", exc_info=True)
        # Clean up temp file if it exists
        if 'temp_file' in locals() and os.path.exists(temp_file):
            os.remove(temp_file)
        raise

# DAG Definition with enhanced configuration
with DAG(
    'iis_etl',
    schedule_interval=timedelta(minutes=5),  # Reduced frequency
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,  # Prevent overlapping runs
    params={
        'raw_log_dir': '/opt/airflow/data/raw_logs',
        'min_files': 1,
        'max_file_age_hours': 24  # Skip files older than this
    },
    default_args={
        'owner': 'airflow',
        'depends_on_past': False,
        'retries': 3,
        'retry_delay': timedelta(minutes=2),
        'max_active_tis_per_dag': MAX_CONCURRENT_PROCESSING,
        'on_failure_callback': lambda context: logger.error(
            f"Task {context['task_instance'].task_id} failed. Exception: {context['exception']}"
        ),
        'execution_timeout': timedelta(hours=1),
        'priority_weight': 10,
    },
    tags=['iis', 'etl', 'security', 'monitoring']
) as dag:
    
    @dag.task
    def validate_environment():
        """Enhanced environment validation"""
        required_dirs = [
            Variable.get("raw_log_dir", "/opt/airflow/data/raw_logs"),
            Variable.get("processing_dir", "/opt/airflow/data/processing"),
            Variable.get("archive_dir", "/opt/airflow/data/archive")
        ]
        
        # Check directory permissions
        for dir_path in required_dirs:
            os.makedirs(dir_path, exist_ok=True)
            if not os.access(dir_path, os.W_OK):
                raise PermissionError(f"Directory not writable: {dir_path}")
        
        # Check GeoIP database
        if not GEOIP_DB_PATH.exists():
            logger.warning(f"GeoIP database not found at {GEOIP_DB_PATH}")
        
        return True
    
    start = EmptyOperator(task_id='start')
    env_check = validate_environment()
    verify_db = PythonOperator(
        task_id='verify_db_connection',
        python_callable=verify_postgres_connection,
        execution_timeout=timedelta(minutes=1)
    )
    
    with TaskGroup("processing") as process:
        
        file_check = PythonOperator(
            task_id='check_files_and_pause',
            python_callable=check_and_pause_dag,
            provide_context=True,
            execution_timeout=timedelta(minutes=1)
        )

        extract_task = PythonOperator(
            task_id='extract',
            python_callable=extract,
            execution_timeout=timedelta(minutes=30),
            do_xcom_push=True
        )

        
        transform_task = PythonOperator(
            task_id='transform',
            python_callable=transform,
            execution_timeout=timedelta(minutes=20),
            do_xcom_push=True
        )

        load_dims = PythonOperator(
            task_id='load_dimensions',
            python_callable=load_dimensions,
            execution_timeout=timedelta(minutes=30)
        )
        
        load_task = PythonOperator(
            task_id='load',
            python_callable=load,
            execution_timeout=timedelta(minutes=45),
            do_xcom_push=True
        )
        
        file_check >> extract_task >> transform_task >> load_dims >> load_task
    
    archive_task = PythonOperator(
        task_id='archive',
        python_callable=archive,
        trigger_rule='all_done',
        execution_timeout=timedelta(minutes=5))
    
    end = EmptyOperator(task_id='end')
    
    # Define dependencies with error handling
    start >> env_check >> verify_db >> process >> archive_task >> end