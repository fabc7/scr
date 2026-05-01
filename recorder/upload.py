import os
import glob
import subprocess
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaFileUpload
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')
STREAM_URL = os.environ.get('STREAM_URL', 'Unknown Stream')

def get_authenticated_service():
    """Autenticarse con YouTube API"""
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    return build('youtube', 'v3', credentials=creds)

def get_video_duration(filepath):
    """Obtener duración del video usando ffprobe"""
    try:
        ffprobe_cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            filepath
        ]
        result = subprocess.run(ffprobe_cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
        return None
    except Exception as e:
        logger.warning(f"Could not get video duration: {e}")
        return None

def validate_video_file(filepath):
    """Validar que el archivo de video sea válido"""
    
    # Verificar que existe
    if not os.path.exists(filepath):
        logger.error(f"Video file does not exist: {filepath}")
        return False
    
    # Verificar tamaño
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if file_size_mb < 1:
        logger.error(f"Video file is too small ({file_size_mb:.2f} MB). Likely corrupted.")
        return False
    
    logger.info(f"File size: {file_size_mb:.2f} MB")
    
    # Verificar duración
    duration = get_video_duration(filepath)
    if duration is None:
        logger.warning("Could not determine video duration. Proceeding anyway...")
        return True  # Permitir que continúe, pero con advertencia
    
    if duration < 10:  # Menos de 10 segundos
        logger.error(f"Video duration is too short ({duration:.1f}s). This video is likely corrupted from the merge process.")
        logger.error("The merge command may have failed. Check recorder.py FFmpeg settings.")
        return False
    
    logger.info(f"Video duration: {duration:.1f}s ({duration/60:.1f} min)")
    return True

def upload_latest_video():
    """Buscar y subir el video más reciente a YouTube"""
    
    # Buscar archivos MP4
    files = glob.glob('**/*.mp4', recursive=True)
    if not files:
        logger.error("No MP4 files found to upload.")
        return False
    
    # Ordenar por fecha de modificación y tomar el más reciente
    files.sort(key=os.path.getmtime)
    video_file = files[-1]
    
    logger.info(f"Video found for upload: {video_file}")
    
    # ============ VALIDACIÓN CRÍTICA ============
    if not validate_video_file(video_file):
        logger.error("Video validation failed. Aborting upload.")
        return False
    
    base_name = os.path.basename(video_file)
    name_without_ext = os.path.splitext(base_name)[0]
    auto_title = name_without_ext.replace('_', ' ')
    
    try:
        # Autenticarse
        logger.info("Authenticating with YouTube...")
        youtube = get_authenticated_service()
        
        # Preparar metadata
        body = {
            'snippet': {
                'title': auto_title,
                'description': f'Auto-recorded stream from: {STREAM_URL}\n\nUploaded via GitHub Actions recorder',
                'tags': ['stream', 'vod', 'recording'],
                'categoryId': '20'  # Gaming category
            },
            'status': {
                'privacyStatus': 'private',  # Privado por defecto
                'selfDeclaredMadeForKids': False
            }
        }
        
        logger.info(f"Uploading video to YouTube: '{auto_title}'")
        
        # Crear request de upload
        media = MediaFileUpload(
            video_file,
            chunksize=1024 * 1024 * 5,  # 5MB chunks
            resumable=True
        )
        request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )
        
        # Ejecutar upload con reintentos
        response = None
        max_retries = 3
        retry_count = 0
        
        while response is None:
            try:
                status, response = request.next_chunk()
                
                if status:
                    progress_percent = int(status.progress() * 100)
                    logger.info(f"Upload progress: {progress_percent}%")
                    
            except Exception as e:
                retry_count += 1
                if retry_count > max_retries:
                    logger.error(f"Upload failed after {max_retries} retries: {e}")
                    return False
                
                logger.warning(f"Upload interrupted (attempt {retry_count}/{max_retries}): {e}")
                logger.info("Retrying...")
                continue
        
        if response and 'id' in response:
            video_id = response.get('id')
            logger.info(f"✓ Upload complete! Video ID: {video_id}")
            logger.info(f"  View at: https://www.youtube.com/watch?v={video_id}")
            return True
        else:
            logger.error("Upload response invalid or missing video ID")
            return False
            
    except Exception as e:
        logger.error(f"YouTube upload failed: {e}")
        logger.error("Check your credentials in environment variables")
        return False

if __name__ == '__main__':
    success = upload_latest_video()
    exit(0 if success else 1)
