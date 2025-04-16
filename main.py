import os
import shutil
import sqlite3
import uuid
# import requests  # <-- 더 이상 사용하지 않으므로 제거
from datetime import datetime
from fastapi import FastAPI, File, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import piexif

# jageocoder 추가
import jageocoder

jageocoder.init(url='https://jageocoder.info-proto.com/jsonrpc')
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


# DB 테이블 생성 (upload_time 컬럼 포함)
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

############################
# jageocoder 초기화 부분 추가
############################

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
    """
    EXIF 내 GPS 정보(DMS)를 10진수(float)로 변환.
    dms 형태: ((도가 분자, 분모), (분의 분자, 분모), (초의 분자, 분모)) 혹은 단순값
    """
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


############################
# jageocoder 기반 역지오코딩
############################
def get_japanese_address_from_latlng(lat, lng):
    """
    jageocoder 라이브러리를 이용해
    (lat, lng)이 일본 내에 있을 경우 주소 풀네임을 반환하고,
    아니라면 '주소 미확인'을 반환.
    """
    try:
        # jageocoder.reverse() 는 (longitude, latitude) 순서
        results = jageocoder.reverse(lng, lat, level=7)
        if results and len(results) > 0:
            # 가장 첫 번째 후보를 사용
            candidate = results[0]['candidate']
            # 예: candidate['fullname'] = ['東京都', '新宿区', ...]
            # join 해서 문자열로 만든다.
            full_address = ''.join(candidate.get('fullname', []))
            return full_address
        else:
            return "주소 미확인"
    except Exception as e:
        print("역지오코딩 에러:", e)
        return "주소 미확인"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, filename, thumbnail_filename, description,
                   latitude, longitude, date_taken, ip_address,
                   device_make, device_model, upload_time
            FROM photos
        """)
        photos = cursor.fetchall()

    photo_list = []
    for photo in photos:
        lat = float(photo["latitude"]) if photo["latitude"] is not None else None
        lng = float(photo["longitude"]) if photo["longitude"] is not None else None

        address = "주소 미확인"
        if lat is not None and lng is not None:
            address = get_japanese_address_from_latlng(lat, lng)

        photo_list.append({
            "id": photo["id"],
            "filename": photo["filename"],
            "thumbnail_filename": photo["thumbnail_filename"],
            "original_filename": photo["filename"],
            "description": photo["description"],
            "latitude": lat,
            "longitude": lng,
            "date_taken": photo["date_taken"],
            "ip_address": photo["ip_address"],
            "device_make": photo["device_make"],
            "device_model": photo["device_model"],
            "upload_time": photo["upload_time"],
            "address": address
        })

    return templates.TemplateResponse("map.html", {
        "request": request,
        "photos": photo_list
    })


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

    # 이미지 EXIF 처리 및 회전
    image = Image.open(file_location)
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

    # 위/경도가 없으면 사용자가 수동 입력한 값 사용 (manual_lat, manual_long)
    if (latitude is None or longitude is None) and manual_lat is not None and manual_long is not None:
        latitude = manual_lat
        longitude = manual_long
    elif latitude is None or longitude is None:
        # 둘 다 없으면 기본값(서울)로 설정
        latitude, longitude = 37.5665, 126.9780

    upload_time = datetime.now().isoformat()

    # DB 삽입
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO photos (
                filename, thumbnail_filename, description,
                latitude, longitude, date_taken, ip_address,
                device_make, device_model, password, upload_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            unique_filename,
            thumbnail_filename,
            description,
            latitude,
            longitude,
            date_taken,
            ip_address,
            device_make,
            device_model,
            password,
            upload_time
        ))
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

        # 삭제
        cursor.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        conn.commit()

        # 실제 파일 삭제
        try:
            os.remove(os.path.join(UPLOAD_DIR, record["filename"]))
            os.remove(os.path.join(THUMBNAIL_DIR, record["thumbnail_filename"]))
        except Exception as e:
            print("ファイル削除中のエラー:", e)

    return JSONResponse({"success": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
