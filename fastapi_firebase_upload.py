from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials, initialize_app, auth
import firebase_admin
import os
from dotenv import load_dotenv

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

@app.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    print("upload callback")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    id_token = authorization.split("Bearer ")[1]
    try:
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token['uid']
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid Firebase token: {str(e)}")

    user_dir = os.path.join(UPLOAD_DIR, uid)
    os.makedirs(user_dir, exist_ok=True)

    file_location = os.path.join(user_dir, file.filename)
    with open(file_location, "wb") as f:
        content = await file.read()
        f.write(content)

    return JSONResponse({
        "success": True,
        "filename": file.filename,
        "uid": uid,
        "url": f"/uploads/{uid}/{file.filename}"  # This will be a direct link to the image
    })
