# fastapi_firebase_upload.py
from fastapi.middleware.cors import CORSMiddleware

from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from fastapi.responses import JSONResponse
from firebase_admin import credentials, initialize_app, auth
import firebase_admin
import os

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8081", "http://127.0.0.1:8081"],  # Use your actual frontend dev URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Firebase Admin SDK
cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ".bon-orlyversaire-firebase-adminsdk-fbsvc-ebc7153a05.json")
if not firebase_admin._apps:
    cred = credentials.Certificate(cred_path)
    initialize_app(cred)

UPLOAD_DIR = "/Users/maayanmoses/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    # Validate Authorization header
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    id_token = authorization.split("Bearer ")[1]
    try:
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token['uid']
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid Firebase token: {str(e)}")

    # Save file to upload directory
    file_location = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_location, "wb") as f:
        content = await file.read()
        f.write(content)

    return JSONResponse({
        "success": True,
        "filename": file.filename,
        "uid": uid,
        "url": f"/uploads/{file.filename}"
    })
