from fastapi import FastAPI, Request, UploadFile, File, Header, Form
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials, initialize_app, auth
from google.cloud import firestore
import firebase_admin
import imghdr
import os
from pathlib import Path
from dotenv import load_dotenv
from typing import Tuple, Optional
import logging
import re
from errors  import raise_error
import asyncio
import sys

# Configure logging to output to stderr (which systemd captures)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)

app = FastAPI()

# Enable CORS (optional for development)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8081", "http://127.0.0.1:8081", "https://bon-orledet.org"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load environment variables from .env file
load_dotenv()

# Global variable to track current domain and credentials
_current_firebase_domain = None
_current_firebase_cred_path = None
_firebase_init_lock = asyncio.Lock() # New: Lock for initialization

def get_credentials_path_for_domain(domain_name: str) -> str:
    """
    Get the appropriate Firebase credentials path based on the domain.
    Uses environment variables with template FIREBASE_CONFIG_PATH_<appname>.
    
    Args:
        domain_name (str): The domain name (e.g., "localhost:8081", "bon-orledet.org")
        
    Returns:
        str: Path to the appropriate service account key file
    """
    # Map domains to their app names for environment variable lookup
    domain_app_map = {
        "localhost:8000": "webair",
        "localhost:8082": "bon_orlyversaire", 
        "bonorledet.org": "bon_orlyversaire",
        "bon-orledet.org": "bon_orlyversaire",
        "iriswebair.com": "webair",
        "iris-webair.com": "webair"
    }
    
    # Get app name for the domain
    app_name = domain_app_map.get(domain_name)
    
    if app_name:
        # Try to get path from environment variable
        env_var_name = f"FIREBASE_CONFIG_PATH_{app_name.upper()}"
        env_path = os.environ.get(env_var_name)
        
        if env_path:
            logging.info(f"Using environment variable {env_var_name} for domain {domain_name}: {env_path}")
            return env_path
    else:
        logging.warning(f"Environment variable {env_var_name} not found for domain {domain_name}, using fallback")
    
    # Fallback to default credentials
    default_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./serviceAccountKey.json")
    logging.info(f"Using default Firebase credentials for domain {domain_name}: {default_path}")
    return default_path

async def initialize_firebase_for_domain(domain_name: str): # Made async
    """
    Initialize Firebase Admin SDK with domain-specific credentials.
    Handles multiple domains by reinitializing with different credentials.
    Ensures thread-safe initialization.
    
    Args:
        domain_name (str): The domain name to get appropriate credentials for
    """
    global _current_firebase_domain, _current_firebase_cred_path
    
    cred_path = get_credentials_path_for_domain(domain_name)
    
    # Check if the domain-specific credential file exists
    if not os.path.exists(cred_path):
        # Fallback to default credentials from env or generic path
        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./serviceAccountKey.json")
        logging.warning(f"Domain-specific credentials not found for {domain_name}, using default: {cred_path}")
        if not os.path.exists(cred_path):
            logging.error(f"Error: Default Firebase credential file not found at {cred_path}. Firebase initialization may fail.")
            # Depending on strictness, you might raise here, or let initialize_app fail

    # Use a lock to ensure only one Firebase initialization happens at a time
    async with _firebase_init_lock:
        # Re-check inside the lock, as _current_firebase_domain might have changed
        if _current_firebase_domain == domain_name and _current_firebase_cred_path == cred_path:
            return
        
        logging.info(f"Initializing Firebase for domain '{domain_name}' with credentials: {cred_path}")
        
        try:
            # Delete existing app if it exists
            if firebase_admin._apps:
                for app_name in list(firebase_admin._apps.keys()):
                    firebase_admin.delete_app(firebase_admin._apps[app_name])
            
            # Initialize with new credentials
            cred = credentials.Certificate(cred_path)
            initialize_app(cred)
            
            # Update global tracking
            _current_firebase_domain = domain_name
            _current_firebase_cred_path = cred_path
            
            logging.info(f"Firebase initialized successfully for domain: {domain_name}")
        except Exception as e:
            # Crucial: If initialization fails here, the server state might be problematic.
            # Consider raising a specific exception or logging prominently.
            logging.error(f"Critical Error: Failed to initialize Firebase for domain {domain_name}: {e}")
            raise # Re-raise to ensure the error is propagated

# Initialize Firebase on application startup
@app.on_event("startup")
async def startup_event():
    # Attempt to initialize Firebase with a default domain on startup
    # This ensures Firebase is ready even before the first request if a domain is known.
    default_domain = os.environ.get("DEFAULT_FIREBASE_DOMAIN", "bon-orledet.org") # Or 'localhost:8081' for dev
    try:
        await initialize_firebase_for_domain(default_domain)
    except Exception as e:
        logging.error(f"Failed to initialize Firebase on startup for default domain {default_domain}: {e}")
        # Depending on criticality, you might want to exit here if Firebase is essential
        # sys.exit(1)

# ... (rest of your FastAPI code from fastapi_firebase_upload.py) ...

# Initialize Firebase with a default domain

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/var/www/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 🔽 Mount static files
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


db = firestore.Client()

# --- Global Constants for Media Handling ---
VALID_IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
VALID_VIDEO_EXTENSIONS = ['.mp4', '.mov', '.avi', '.webm', '.mkv']
VALID_IMAGE_MIMES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
VALID_VIDEO_MIMES = ['video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/webm', 'video/x-matroska']

# Retrieve max sizes from environment variables, with defaults
MAX_IMAGE_SIZE_MB = int(os.environ.get("MAX_IMAGE_SIZE_MB", 5)) # Default 5 MB for images
MAX_VIDEO_SIZE_MB = int(os.environ.get("MAX_VIDEO_SIZE_MB", 100)) # Default 100 MB for videos

# Convert to bytes
MAX_IMAGE_SIZE_BYTES = MAX_IMAGE_SIZE_MB * 1024 * 1024
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
# --- End Global Constants ---

def ensure_admin_role(uid: str):
    user_data = db.collection("users").document(uid).get()
    
    if user_data.exists:
        user_role = user_data.to_dict().get("role")
        if user_role != "admin":
            raise_error("NOT_ADMIN")
    else:
        raise_error("USER_NOT_FOUND")

@app.get("/config")
async def get_config():
    max_images_per_wish = int(os.environ.get("MAX_IMAGES_PER_WISH", 5))
    max_wishes_per_user = int(os.environ.get("MAX_WISHES_PER_USER", 5))

    return JSONResponse({
        "max_images_per_wish": max_images_per_wish,
        "max_wishes_per_user": max_wishes_per_user
    })

@app.post("/upload-site-image")
async def upload_site_image(
    request: Request,
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    """
    Endpoint to upload a site-wide image, restricted to admins.
    
    Args:
        request (Request): The incoming HTTP request.
        file (UploadFile): The uploaded image file.
        authorization (str): Authorization header with Firebase token.
        
    Form Data or Query Parameters (optional):
        relative_path (str): Custom relative path for site images (defaults to "general")

    Returns:
        JSONResponse: Success status, filename, and URL of uploaded image.
    """
    uid = await authenticate_user(authorization, request)

    ensure_admin_role(uid)
    
    # Get all form data
    form_data = await request.form()
    # Access a specific field
    relative_path = form_data.get("relative_path", "general")  # Default to "general" if not provided
    
    # For site images, we don't use uid (None), only the relative_path
    upload_dir = get_file_path(request, None, relative_path)

    logging.info(f"Site image upload dir: {upload_dir}")

    saved_filename, media_type = await validate_and_save_media(file, upload_dir)

    rel_dir = get_image_relative_dir(request, None, relative_path)

    return JSONResponse({
        "success": True,
        "filename": saved_filename,
        "url": f"/uploads/{rel_dir}/{saved_filename}",
        "relative_path": relative_path
    })

@app.post("/upload")
async def upload_image(
    request: Request,
    relative_path: str = None,
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    """
    Endpoint for authenticated users to upload images, with limits on number and size.

    Args:
        request (Request): The incoming HTTP request.
        relative_path (str): The relative path within the user's directory (e.g., "wishes/wish123" or "blog/posts/2024/01").
        file (UploadFile): The uploaded image file.
        authorization (str): Authorization header with Firebase token.

    Returns:
        JSONResponse: Success status, original filename, user ID, and image URL.
    """
    logging.info("upload callback")

    uid = await authenticate_user(authorization, request)

    form_data = await request.form()

    relative_path = form_data.get("wish_id") or relative_path

    logging.info(f"relative_path: {relative_path}")

    upload_dir = get_file_path(request, uid, relative_path)

    logging.info(f"Upload dir: {upload_dir}")

    MAX_IMAGES_PER_PATH = int(os.environ.get("MAX_IMAGES_PER_WISH", 5))

    if (await count_media_files_in_dir(upload_dir) >= MAX_IMAGES_PER_PATH):
        raise_error("LIMIT_REACHED")
    
    saved_filename, media_type = await validate_and_save_media(file, upload_dir)

    rel_dir = get_image_relative_dir(request, uid, relative_path)

    return JSONResponse({
        "success": True,
        "filename": saved_filename,
        "uid": uid,
        "url": f"/uploads/{rel_dir}/{saved_filename}",  # This will be a direct link to the image
        "max_images": MAX_IMAGES_PER_PATH
    })

@app.delete("/delete-site-image")
async def delete_site_image(
    request: Request,
    filename: str,
    authorization: str = Header(None)
):
    """
    Endpoint to delete a site-wide image, restricted to admins.
    
    Args:
        request (Request): The incoming HTTP request.
        filename (str): Name of the file to delete.
        authorization (str): Authorization header with Firebase token.
        
    Query Parameters (optional):
        relative_path (str): Custom relative path for site images (defaults to "general")

    Returns:
        JSONResponse: Success status and deletion confirmation message.
    """
    uid = await authenticate_user(authorization, request)

    ensure_admin_role(uid)
    
    # Get relative_path from query parameters, default to "general"
    relative_path = request.query_params.get("relative_path", "general")
    
    file_path = get_file_path(request, None, relative_path, filename)

    logging.info(f"Deleting site image: {file_path}")

    remove_file(file_path)

    return JSONResponse({
        "success": True,
        "message": f"File '{filename}' deleted successfully",
        "relative_path": relative_path
    })


@app.delete("/delete-image")
async def delete_image(
    request: Request,
    relative_path: str,
    filename: str,
    authorization: str = Header(None)
):
    """
    Endpoint to delete a user's uploaded image.

    Args:
        request (Request): The incoming HTTP request.
        relative_path (str): The relative path within the user's directory.
        filename (str): Name of the file to delete.
        authorization (str): Authorization header with Firebase token.

    Returns:
        JSONResponse: Success status and deletion confirmation message.
    """
    uid = await authenticate_user(authorization, request)

    # Get form data and query parameters for backward compatibility
    form_data = await request.form()
    query_params = request.query_params
    
    # Priority: form data > query param > function arg > legacy wish_id > default
    relative_path = (
        form_data.get("relative_path") or 
        query_params.get("relative_path") or 
        relative_path or 
        form_data.get("wish_id") or 
        query_params.get("wish_id") or 
        "general"
    )

    file_path = get_file_path(request, uid, relative_path, filename)

    logging.info(f"File path: {file_path}")

    remove_file(file_path)

    return JSONResponse({
        "success": True,
        "message": f"File '{filename}' deleted successfully"
    })

def remove_file(file_path):
    if not os.path.isfile(file_path):
        raise_error("FILE_NOT_FOUND")

    try:
        os.remove(file_path)
    except Exception as e:
        logging.exception(f"delete file error {str(e)}")
        raise_error("DELETE_FAILED")

async def is_valid_media_type(file: UploadFile) -> Tuple[bool, Optional[str]]:
    """
    Checks if the uploaded file is a valid image or video based on its MIME type and extension.

    Args:
        file (UploadFile): The uploaded file object.

    Returns:
        tuple[bool, str | None]: (True if valid, media type category 'image'/'video' or None)
    """
    # Check extension first
    ext = Path(file.filename).suffix.lower()
    mime = file.content_type.lower()
    
    is_image_ext = ext in VALID_IMAGE_EXTENSIONS
    is_video_ext = ext in VALID_VIDEO_EXTENSIONS
    is_image_mime = mime in VALID_IMAGE_MIMES
    is_video_mime = mime in VALID_VIDEO_MIMES

    # Determine category based on MIME type primarily, then extension as fallback
    media_category = None
    if is_image_mime and not is_video_mime: # Explicitly image, not also video
        media_category = 'image'
    elif is_video_mime and not is_image_mime: # Explicitly video, not also image
        media_category = 'video'
    elif is_image_mime and is_video_mime: # Ambiguous MIME (unlikely for standard types)
        # Fallback to extension if MIME is ambiguous
        if is_image_ext and not is_video_ext:
            media_category = 'image'
        elif is_video_ext and not is_image_ext:
            media_category = 'video'
        else: # Still ambiguous or both (e.g., a file with '.mp4' extension but 'image/jpeg' MIME if spoofed)
            logging.info(f"Ambiguous media type for {file.filename}: MIME={mime}, Ext={ext}. Defaulting to None.")
            return (False, None) # Consider it invalid if truly ambiguous
    else: # MIME is not explicitly image or video
        # Try to infer from extension if MIME is generic or unknown
        if is_image_ext:
            media_category = 'image'
        elif is_video_ext:
            media_category = 'video'
        else:
            return (False, None) # Neither valid MIME nor valid extension

    # For images, perform a deeper content check using imghdr
    if media_category == 'image':
        file.file.seek(0)
        head = await file.read(512)
        file.file.seek(0) # Reset file pointer for subsequent reads
        kind = imghdr.what(None, head)
        if kind not in ['jpeg', 'png', 'gif']: # imghdr doesn't support webp, so rely on mime/ext for webp
             if ext == '.webp': # Special case for webp which imghdr doesn't recognize
                 if mime == 'image/webp':
                     return (True, 'image')
             return (False, None) # Not a valid image by content inspection (and not webp)
        return (True, 'image')
    elif media_category == 'video':
        # For video, MIME and extension check is usually sufficient for common types
        return (True, 'video')
    
    return (False, None) # Should ideally not be reached if logic is complete

async def count_media_files_in_dir(folder_path: str) -> int:
    """
    Count the number of valid image and video files in a given directory.

    Args:
        folder_path (str): Path to the directory.

    Returns:
        int: Number of valid media files.
    """
    folder = Path(folder_path)
    if not folder.exists():
        return 0

    media_files_count = 0
    for file_path in folder.iterdir():
        if file_path.is_file():
            try:
                # Check file extension first
                ext = file_path.suffix.lower()
                
                # Determine content type based on extension
                content_type = "application/octet-stream"
                if ext in VALID_IMAGE_EXTENSIONS:
                    if ext == '.jpg' or ext == '.jpeg':
                        content_type = 'image/jpeg'
                    elif ext == '.png':
                        content_type = 'image/png'
                    elif ext == '.gif':
                        content_type = 'image/gif'
                    elif ext == '.webp':
                        content_type = 'image/webp'
                elif ext in VALID_VIDEO_EXTENSIONS:
                    if ext == '.mp4':
                        content_type = 'video/mp4'
                    elif ext == '.mov':
                        content_type = 'video/quicktime'
                    elif ext == '.avi':
                        content_type = 'video/x-msvideo'
                    elif ext == '.webm':
                        content_type = 'video/webm'
                    elif ext == '.mkv':
                        content_type = 'video/x-matroska'

                # Create UploadFile with proper content type in headers
                temp_upload_file = UploadFile(
                    filename=file_path.name,
                    file=file_path.open("rb"),
                    headers={"content-type": content_type}
                )

                is_valid, _ = await is_valid_media_type(temp_upload_file)
                temp_upload_file.file.close() # Close the file handle

                if is_valid:
                    media_files_count += 1
            except Exception as e:
                logging.warning(f"Error checking file {file_path.name} for media type: {e}")
                # This error means we couldn't even determine its type, so we don't count it.
    
    logging.info(f"Total valid media files in {folder_path}: {media_files_count}")
    return media_files_count

async def validate_and_save_media(file: UploadFile, dest_dir: str) -> tuple[str, str]:
    """
    Validate the uploaded media file and save it to the destination directory.

    Args:
        file (UploadFile): The uploaded file.
        dest_dir (str): The destination directory path.

    Raises:
        HTTPException: If the file is not a valid media or is too large.

    Returns:
        tuple[str, str]: (The safe filename of the saved media, the media type 'image' or 'video')
    """
    is_valid, media_type = await is_valid_media_type(file)

    if not is_valid or media_type is None:
        raise_error("INVALID_MEDIA_TYPE") # Generic error for now

    content = await file.read() # Read content AFTER type validation for safety and efficiency

    file_size_bytes = len(content)

    if media_type == 'image' and file_size_bytes > MAX_IMAGE_SIZE_BYTES:
        raise_error("IMAGE_TOO_LARGE")
    elif media_type == 'video' and file_size_bytes > MAX_VIDEO_SIZE_BYTES:
        raise_error("VIDEO_TOO_LARGE")
    elif media_type not in ['image', 'video']: # Should ideally be caught by is_valid_media_type, but a safeguard
        raise_error("INVALID_MEDIA_TYPE")


    safe_filename = Path(file.filename).name # Use original filename as safe name for simplicity
    file_location = os.path.join(dest_dir, safe_filename)

    os.makedirs(dest_dir, exist_ok=True)

    try:
        with open(file_location, "wb") as f:
            f.write(content)
        logging.info(f"Saved {media_type} file: {file_location}")
    except OSError as e:
        logging.exception(f"Failed to save file to disk: {file_location} - {str(e)}")
        raise_error("FILE_SAVE_FAILED") # Raise the new error for file saving failure

    logging.info(f"Saved {media_type} file: {file_location}")
    return safe_filename, media_type

async def authenticate_user(authorization: str, request: Request = None):
    """
        Verify the Firebase ID token from the Authorization header and return the user ID.
        Uses domain-specific Firebase credentials if request is provided.

        Args:
            authorization (str): The 'Authorization' header value expected to be 'Bearer <token>'.
            request (Request, optional): The incoming HTTP request to determine domain.

        Raises:
            HTTPException: If the authorization header is missing or malformed.
            HTTPException: If the Firebase token is invalid.

        Returns:
            str: The Firebase user ID (uid) extracted from the token.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise_error("MISSING_AUTH")

    # Initialize Firebase with domain-specific credentials if request is provided
    if request:
        domain_name = get_safe_domain_name(request)
        try:
            # Re-initialize Firebase for this specific domain
            await initialize_firebase_for_domain(domain_name)
        except Exception as e:
                    logging.warning(f"Could not initialize domain-specific Firebase for {domain_name}: {e}")
        # Continue with existing Firebase instance

    id_token = authorization.split("Bearer ")[1]
    try:
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token['uid']
    except Exception as e:
        raise_error("INVALID_TOKEN")

def get_file_path(request: Request, uid: str = None, relative_path: str = None, filename: str = None):
    """
    Construct the user's upload directory or full file path.

    Args:
        request (Request): Incoming HTTP request to get the domain name.
        uid (str): User ID.
        relative_path (str): The relative path within the user's directory.
        filename (str, optional): Specific filename to build full path.

    Returns:
        str: Path to user directory or to the specific file.
    """
    base_path = Path(UPLOAD_DIR) / get_image_relative_dir(request, uid, relative_path)

    base_path.mkdir(parents=True, exist_ok=True)

    if filename:
        base_path = base_path / Path(filename).name

    logging.info(f"path: {str(base_path)}")
    return str(base_path)

def get_image_relative_dir(request: Request, uid: str, relative_path: str = None):
    domain_name = get_safe_domain_name(request)

    path = Path(domain_name)
    if uid:
        path = path / Path(uid)

    if relative_path:
        path = path / Path(relative_path)

    return path

def get_safe_domain_name(request: Request):
    domain_name = request.headers.get("host")

    return re.sub(r"[^a-zA-Z0-9.-:]", "", domain_name)
