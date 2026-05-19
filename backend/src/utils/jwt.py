import os
from pathlib import Path
from jose import jwt, JWTError
from fastapi import HTTPException, Header, Depends
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv(Path(__file__).parent.parent.parent / ".env")
JWT_KEY = os.getenv("JWT_KEY")
ALGORITHM = os.getenv("ALGORITHM")

def create_access_token(user_id : int) -> str:
    payload = {
        "sub" : str(user_id),
        "exp" : datetime.utcnow() + timedelta(hours = 2)
    }
    token = jwt.encode(payload, JWT_KEY, ALGORITHM)
    return token

def get_user_id_from_token(token : str | None = Header(None) ) -> int :
    if not token :
        raise HTTPException(401, "未携带Token")
    try :
        payload = jwt.decode(token, JWT_KEY, [ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise ValueError
        return int(user_id)

    except (ValueError, JWTError, TypeError) :
        raise HTTPException(401, "toekn无效或已过期")
