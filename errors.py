from fastapi import HTTPException

ERROR_CODES = {
    "INVALID_TOKEN": {
        "code": 1001,
        "message": "Invalid Firebase token",
        "http": 401  # Unauthorized
    },
    "MISSING_AUTH": {
        "code": 1002,
        "message": "Missing or invalid Authorization header",
        "http": 401  # Unauthorized
    },
    "NOT_ADMIN": {
        "code": 1003,
        "message": "Only admins can upload site images",
        "http": 403  # Forbidden
    },
    "USER_NOT_FOUND": {
        "code": 1004,
        "message": "User not found",
        "http": 404  # Not Found
    },
    "INVALID_IMAGE": {
        "code": 1005,
        "message": "Uploaded file is not a valid image",
        "http": 400  # Bad Request
    },
    "IMAGE_TOO_LARGE": {
        "code": 1006,
        "message": "Image exceeds allowed size",
        "http": 413  # Payload Too Large
    },
    "LIMIT_REACHED": {
        "code": 1007,
        "message": "Image limit reached for this user",
        "http": 403  # Forbidden
    },
    "FILE_NOT_FOUND": {
        "code": 1008,
        "message": "File not found",
        "http": 404  # Not Found
    },
    "DELETE_FAILED": {
        "code": 1009,
        "message": "Failed to delete file",
        "http": 500  # Internal Server Error
    }
}

def raise_error(key: str):
    error = ERROR_CODES.get(key)
    if not error:
        raise HTTPException(status_code=500, detail={"error_code": 9999, "message": "Unknown error"})
    raise HTTPException(status_code=error["http"], detail={
        "error_code": error["code"],
        "message": error["message"]
    })

