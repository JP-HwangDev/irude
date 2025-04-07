import os
import shutil
import sqlite3
import uuid
from datetime import datetime
from fastapi import FastAPI, File, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import piexif  # piexif 라이브러리 추가

app = FastAPI()

UPLOAD_DIR = "uploads"
THUMBNAIL_DIR = "uploads/thumbnails"
for directory in [UPLOAD_DIR, THUMBNAIL_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

templates = Jinja2Templates(directory="templates")


def get_db_connection():
    conn = sqlite3.connect("photos.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def debug_print(message):
    print(message)


# DB 테이블 생성 (upload_time 컬럼 추가됨)
with get_db_connection() as conn:
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            thumbnail_filename TEXT,
            description TEXT,
            latitude REAL,
            longitude REAL,
            date_taken TEXT,
            ip_address TEXT,
            device_make TEXT,
            device_model TEXT,
            password TEXT,
            upload_time TEXT
        )
    ''')
    conn.commit()


def get_exif_data(image):
    exif_data = {}
    info = image._getexif()
    if info:
        for tag, value in info.items():
            decoded = TAGS.get(tag, tag)
            exif_data[decoded] = value
    return exif_data


def get_geotagging(exif_data):
    if not exif_data or "GPSInfo" not in exif_data:
        return None
    gps_info = exif_data["GPSInfo"]
    if not isinstance(gps_info, dict):
        return None
    geotagging = {}
    for key, value in gps_info.items():
        decode = GPSTAGS.get(key, key)
        geotagging[decode] = value
    return geotagging


def get_decimal_from_dms(dms, ref):
    if isinstance(dms[0], tuple):
        degrees = dms[0][0] / dms[0][1]
        minutes = dms[1][0] / dms[1][1]
        seconds = dms[2][0] / dms[2][1]
    else:
        degrees = float(dms[0])
        minutes = float(dms[1])
        seconds = float(dms[2])
    decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
    if ref in ['S', 'W']:
        decimal = -decimal
    return decimal


def create_thumbnail(original_path, thumbnail_path, max_size=(200, 200)):
    with Image.open(original_path) as img:
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        img.save(thumbnail_path, "JPEG", quality=85)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, filename, thumbnail_filename, description, latitude, longitude, date_taken, ip_address, device_make, device_model, upload_time FROM photos")
        photos = cursor.fetchall()

    photo_list = [
        {
            "id": photo["id"],
            "filename": photo["filename"],
            "thumbnail_filename": photo["thumbnail_filename"],
            "original_filename": photo["filename"],
            "description": photo["description"],
            "latitude": float(photo["latitude"]),
            "longitude": float(photo["longitude"]),
            "date_taken": photo["date_taken"],
            "ip_address": photo["ip_address"],
            "device_make": photo["device_make"],
            "device_model": photo["device_model"],
            "upload_time": photo["upload_time"]
        } for photo in photos
    ]
    return templates.TemplateResponse("map.html", {"request": request, "photos": photo_list})


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/upload")
async def upload_photo(
        request: Request,
        file: UploadFile = File(...),
        description: str = Form(...),
        password: str = Form(...),
        manual_lat: float = Form(None),
        manual_long: float = Form(None)
):
    if len(password) < 8:
        return HTMLResponse("パスワードは8文字以上必要です。", status_code=400)

    ip_address = request.client.host

    # 고유 파일 이름 생성
    _, ext = os.path.splitext(file.filename)
    unique_filename = f"{uuid.uuid4().hex}{ext}"
    file_location = os.path.join(UPLOAD_DIR, unique_filename)

    # 파일 저장
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 이미지 열기
    image = Image.open(file_location)

    # EXIF 처리 및 이미지 회전
    try:
        exif_bytes = image.info.get('exif')
        if exif_bytes:
            exif_dict = piexif.load(exif_bytes)
            orientation = exif_dict["0th"].get(piexif.ImageIFD.Orientation, 1)
            if orientation == 3:
                image = image.rotate(180, expand=True)
            elif orientation == 6:
                image = image.rotate(270, expand=True)
            elif orientation == 8:
                image = image.rotate(90, expand=True)
            exif_dict["0th"][piexif.ImageIFD.Orientation] = 1
            new_exif_bytes = piexif.dump(exif_dict)
            image.save(file_location, quality=95, exif=new_exif_bytes)
        else:
            image.save(file_location, quality=95)
    except Exception as e:
        print("EXIF 처리 에러:", e)
        image.save(file_location, quality=95)

    # 썸네일 생성
    thumbnail_filename = f"thumb_{unique_filename}"
    thumbnail_location = os.path.join(THUMBNAIL_DIR, thumbnail_filename)
    create_thumbnail(file_location, thumbnail_location)

    # EXIF 데이터 추출
    image = Image.open(file_location)
    exif_data = get_exif_data(image)
    geotagging = get_geotagging(exif_data)
    latitude = None
    longitude = None
    date_taken = exif_data.get("DateTimeOriginal") if exif_data else None
    device_make = exif_data.get("Make", "Unknown") if exif_data else "Unknown"
    device_model = exif_data.get("Model", "Unknown") if exif_data else "Unknown"

    if geotagging:
        if "GPSLatitude" in geotagging and "GPSLatitudeRef" in geotagging:
            latitude = get_decimal_from_dms(geotagging["GPSLatitude"], geotagging["GPSLatitudeRef"])
        if "GPSLongitude" in geotagging and "GPSLongitudeRef" in geotagging:
            longitude = get_decimal_from_dms(geotagging["GPSLongitude"], geotagging["GPSLongitudeRef"])

    if (latitude is None or longitude is None) and manual_lat is not None and manual_long is not None:
        latitude = manual_lat
        longitude = manual_long
    elif latitude is None or longitude is None:
        latitude, longitude = 37.5665, 126.9780  # 기본값: 서울

    upload_time = datetime.now().isoformat()

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO photos (filename, thumbnail_filename, description, latitude, longitude, date_taken, ip_address, device_make, device_model, password, upload_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            unique_filename, thumbnail_filename, description, latitude, longitude, date_taken, ip_address,
            device_make, device_model, password, upload_time))
        conn.commit()

    return RedirectResponse("/", status_code=303)


@app.post("/delete")
async def delete_photo(photo_id: int = Form(...), password: str = Form(...)):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT password, filename, thumbnail_filename FROM photos WHERE id = ?", (photo_id,))
        record = cursor.fetchone()
        if not record:
            return JSONResponse({"success": False, "error": "写真が見つかりません。"})
        if record["password"] != password:
            return JSONResponse({"success": False, "error": "パスワードが間違っています。"})
        cursor.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        conn.commit()
        try:
            os.remove(os.path.join(UPLOAD_DIR, record["filename"]))
            os.remove(os.path.join(THUMBNAIL_DIR, record["thumbnail_filename"]))
        except Exception as e:
            print("ファイル削除中のエラー:", e)
    return JSONResponse({"success": True})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)