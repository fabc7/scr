import os
import glob
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaFileUpload

CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')
STREAM_URL = os.environ.get('STREAM_URL', 'Unknown Stream')
VIDEO_TITLE = os.environ.get('VIDEO_TITLE', 'Auto Upload')

def get_authenticated_service():
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    return build('youtube', 'v3', credentials=creds)

def upload_latest_video():
    # Busca recursivamente cualquier archivo .mp4 en el repositorio
    files = glob.glob('**/*.mp4', recursive=True)
    if not files:
        print("[ERROR] No MP4 files found to upload.")
        return

    # Toma el archivo modificado más recientemente
    files.sort(key=os.path.getmtime)
    video_file = files[-1]
    print(f"[INFO] Video found for upload: {video_file}")

    try:
        youtube = get_authenticated_service()
        body = {
            'snippet': {
                'title': VIDEO_TITLE,
                'description': f'Auto re-upload from {STREAM_URL}',
                'tags': ['stream', 'vod'],
                'categoryId': '20' # 20 = Gaming
            },
            'status': {
                'privacyStatus': 'private', # Se sube en privado por defecto
                'selfDeclaredMadeForKids': False
            }
        }
        
        print(f"[INFO] Uploading video to YouTube as '{VIDEO_TITLE}'...")
        # chunksize establecido en ~5MB para no sobrecargar la RAM del servidor
        media = MediaFileUpload(video_file, chunksize=1024*1024*5, resumable=True) 
        request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"Upload Progress: {int(status.progress() * 100)}%")
                
        print(f"\n[SUCCESS] Upload complete! Video ID: {response.get('id')}")
        
    except Exception as e:
        print(f"[ERROR] YouTube Upload failed: {e}")

if __name__ == '__main__':
    upload_latest_video()
