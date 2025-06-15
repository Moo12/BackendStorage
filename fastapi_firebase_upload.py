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

# Firebase Admin Init
cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./serviceAccountKey.json")
if not firebase_admin._apps:
    cred = credentials.Certificate(cred_path)
    initialize_app(cred)

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

    Returns:
        JSONResponse: Success status, filename, and URL of uploaded image.
    """
    uid = authenticate_user(authorization)

    ensure_admin_role(uid)

    general_dir = get_file_path(request, "general")

    print(f"general dir {general_dir}")

    saved_filename, media_type = await validate_and_save_media(file, general_dir)

    rel_dir = get_image_relative_dir(request, "general")

    return JSONResponse({
        "success": True,
        "filename": saved_filename,
        "url": f"/uploads/{rel_dir}/{saved_filename}",
    })

@app.post("/upload")
async def upload_image(
    request: Request,
    wish_id: str = Form(...),
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    """
    Endpoint for authenticated users to upload images, with limits on number and size.

    Args:
        request (Request): The incoming HTTP request.
        file (UploadFile): The uploaded image file.
        authorization (str): Authorization header with Firebase token.

    Returns:
        JSONResponse: Success status, original filename, user ID, and image URL.
    """
    logging.info("upload callback")

    uid = authenticate_user(authorization)

    wish_dir = get_file_path(request, uid, wish_id)

    print(f"uid dir {wish_dir}")

    MAX_IMAGES_PER_WISH = int(os.environ.get("MAX_IMAGES_PER_WISH", 5))

    if (await count_media_files_in_dir(wish_dir) >= MAX_IMAGES_PER_WISH):
        raise_error("LIMIT_REACHED")
    
    saved_filename, media_type = await validate_and_save_media(file, wish_dir)



    rel_dir = get_image_relative_dir(request, uid, wish_id)

    return JSONResponse({
        "success": True,
        "filename": saved_filename,
        "uid": uid,
        "url": f"/uploads/{rel_dir}/{saved_filename}",  # This will be a direct link to the image
        "max_images": MAX_IMAGES_PER_WISH
    })

@app.delete("/delete-site-image")
async def delete_site_image(
    request: Request,
    filename: str,
    authorization: str = Header(None)
):
    
    uid = authenticate_user(authorization)

    ensure_admin_role(uid)
    
    file_path = get_file_path(request, "general", None, filename)

    logging.info(file_path)

    remove_file(file_path)

    return JSONResponse({
        "success": True,
        "message": f"File '{filename}' deleted successfully"
    })


@app.delete("/delete-image")
async def delete_image(
    request: Request,
    wish_id: str,
    filename: str,
    authorization: str = Header(None)
):
    """
    Endpoint to delete a user's uploaded image.

    Args:
        request (Request): The incoming HTTP request.
        filename (str): Name of the file to delete.
        authorization (str): Authorization header with Firebase token.

    Returns:
        JSONResponse: Success status and deletion confirmation message.
    """
    uid = authenticate_user(authorization)

    file_path = get_file_path(request, uid, wish_id, filename)

    logging.info(file_path)

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
            logger.warning(f"Ambiguous media type for {file.filename}: MIME={mime}, Ext={ext}. Defaulting to None.")
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
            # Create a dummy UploadFile object for is_valid_media_type check
            # This is a workaround as is_valid_media_type expects UploadFile.
            # A more robust solution might refactor is_valid_media_type to take bytes/path.
            try:
                # Read enough bytes for imghdr if it's an image
                with open(file_path, "rb") as f:
                    file_content_sample = f.read(512) # Read small sample for imghdr check
                    f.seek(0) # Reset for potential full read if needed by future checks

                # Simulate UploadFile attributes
                temp_upload_file = UploadFile(
                    filename=file_path.name,
                    file=file_path.open("rb"), # Pass actual file handle
                    headers={"content-type": "application/octet-stream"} # Placeholder, will be determined by is_valid_media_type
                )
                
                # Try to guess mime type to pass to is_valid_media_type for better check
                # This is a simplification; in a real scenario, you might infer MIME from extension
                # or use a library like python-magic. For now, we rely on suffix for `is_valid_media_type`'s logic.
                ext = file_path.suffix.lower()
                if ext in VALID_IMAGE_EXTENSIONS:
                    temp_upload_file.content_type = VALID_IMAGE_MIMES[0] if '.jpeg' in VALID_IMAGE_EXTENSIONS else "image/jpeg" # Arbitrary default
                elif ext in VALID_VIDEO_EXTENSIONS:
                    temp_upload_file.content_type = VALID_VIDEO_MIMES[0] if '.mp4' in VALID_VIDEO_EXTENSIONS else "video/mp4" # Arbitrary default
                else:
                     # If neither, it's unlikely to be valid media for our purpose, but let is_valid_media_type decide
                     temp_upload_file.content_type = "application/octet-stream"

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

def authenticate_user(authorization: str):
    """
        Verify the Firebase ID token from the Authorization header and return the user ID.

        Args:
            authorization (str): The 'Authorization' header value expected to be 'Bearer <token>'.

        Raises:
            HTTPException: If the authorization header is missing or malformed.
            HTTPException: If the Firebase token is invalid.

        Returns:
            str: The Firebase user ID (uid) extracted from the token.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise_error("MISSING_AUTH")

    id_token = authorization.split("Bearer ")[1]
    try:
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token['uid']
    except Exception as e:
        raise_error("INVALID_TOKEN")

def get_file_path(request: Request, uid: str, wish_id: str = None, filename: str = None):
    """
    Construct the user's upload directory or full file path.

    Args:
        request (Request): Incoming HTTP request to get the domain name.
        uid (str): User ID.
        wish_id: Wish Id
        filename (str, optional): Specific filename to build full path.

    Returns:
        str: Path to user directory or to the specific file.
    """
    base_path = Path(UPLOAD_DIR) / get_image_relative_dir(request, uid, wish_id)

    base_path.mkdir(parents=True, exist_ok=True)

    if filename:
        base_path = base_path / Path(filename).name

    print(f"path: {str(base_path)}")
    return str(base_path)

def get_image_relative_dir(request: Request, uid: str, wish_id: str = None):
    domain_name = get_safe_domain_name(request)

    relative_path = Path(domain_name) / Path(uid)

    if wish_id:
        relative_path = relative_path / Path(wish_id)

    return relative_path

def get_safe_domain_name(request: Request):
    domain_name = request.headers.get("host")

    return re.sub(r"[^a-zA-Z0-9.-:]", "", domain_name)
