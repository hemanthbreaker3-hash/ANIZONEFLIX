import jwt
import datetime
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer
from config.config import Config

security = HTTPBearer()

def create_access_token(data: dict, expires_delta: datetime.timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.datetime.utcnow() + expires_delta
    else:
        expire = datetime.datetime.utcnow() + datetime.timedelta(hours=24)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, Config.SECRET_KEY, algorithm="HS256")
    return encoded_jwt

def verify_token(token: str):
    if not token:
        return None
    try:
        payload = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except (jwt.InvalidTokenError, jwt.DecodeError):
        return None
    except Exception:
        return None

async def get_current_admin(request: Request):
    # Try to get token from cookie or header
    token = request.cookies.get("admin_token")
    if not token:
        # Check header
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = verify_token(token)
    if not payload or not payload.get("is_admin"):
        raise HTTPException(status_code=403, detail="Not authorized")

    return payload
