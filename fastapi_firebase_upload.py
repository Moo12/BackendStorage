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

    saved_filename = await validate_and_save_image(file, general_dir)

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

    logging.info(f"uid dir {wish_dir}")

    MAX_IMAGES_PER_WISH = int(os.environ.get("MAX_IMAGES_PER_WISH", 5))

    if (await count_images_files_in_dir(wish_dir) >= MAX_IMAGES_PER_WISH):
        raise_error("LIMIT_REACHED")
    
    saved_filename = await validate_and_save_image(file, wish_dir)

    rel_dir = get_image_relative_dir(request, uid, wish_id)

    return JSONResponse({
        "success": True,
        "filename": file.filename,
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

async def is_valid_image(file: UploadFile) -> bool:
    # Check extension
    print("203")

    valid_exts = ['.jpg', '.jpeg', '.png', '.gif']
    ext = Path(file.filename).suffix.lower()
    if ext not in valid_exts:
        return False
    
    # Check MIME
    mime = file.content_type.lower()
    if not mime.startswith('image/'):
        return False
    
    # Read first 512 bytes to check content
    file.file.seek(0)
    head = await file.read(512)
    file.file.seek(0)

    kind = imghdr.what(None, head)
    return kind in ['jpeg', 'png', 'gif']

async def count_images_files_in_dir(folder_path: str) -> int:
    """
    Count the number of valid image files in a given directory.

    Args:
        folder_path (str): Path to the directory.

    Returns:
        int: Number of valid image files.
    """
    folder = Path(folder_path)
    image_files = []

    for file_path in folder.iterdir():
        if file_path.is_file():
            try:
                with file_path.open("rb") as f:
                    if await is_valid_image(f.read()):
                        image_files.append(file_path.name)
            except Exception as e:
                logging.warning(f"Error reading file {file_path}: {e}")

    logging.info(f"Valid image files: {image_files}")
    return len(image_files)


async def validate_and_save_image(file: UploadFile, dest_dir: str) -> str:
    """
    Validate the uploaded image file and save it to the destination directory.

    Args:
        file (UploadFile): The uploaded file.
        dest_dir (str): The destination directory path.

    Raises:
        HTTPException: If the file is not a valid image or is too large.

    Returns:
        str: The safe filename of the saved image.
    """
    MAX_IMAGE_SIZE_MB = int(os.environ.get("MAX_IMAGE_SIZE_MB", 5))
    MAX_FILE_SIZE_BYTES = MAX_IMAGE_SIZE_MB * 1024 * 1024

    content = await file.read()

    is_valid_image_res = await is_valid_image(file)

    # Validate image type
    if not is_valid_image_res:
        raise_error("INVALID_IMAGE")

    if len(content) > MAX_FILE_SIZE_BYTES:
        raise_error("IMAGE_TOO_LARGE")

    safe_filename = Path(file.filename).name
    file_location = os.path.join(dest_dir, safe_filename)

    os.makedirs(dest_dir, exist_ok=True)

    with open(file_location, "wb") as f:
        f.write(content)

    return safe_filename

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
        return str(base_path / Path(filename).name)

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
