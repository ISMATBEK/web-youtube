import os
import threading
import time
import uuid
import json
from flask import Flask, request, jsonify, send_from_directory, render_template
# yt-dlp tez va ishonchli yuklash uchun ishlatiladi
from yt_dlp import YoutubeDL, DownloadError
from concurrent.futures import ThreadPoolExecutor

# Flask ilovasini sozlash
app = Flask(__name__)

# Yuklab olingan fayllarni saqlash uchun katalog
DOWNLOAD_DIR = 'downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Vazifa holatini saqlash uchun lug'at (Task Queue simulatsiyasi)
# Kalit: task_id, Qiymat: {'status': 'processing', 'progress': 0, 'file_info': {...}, 'error': None}
task_registry = {}

# Asinxron vazifalarni boshqarish uchun Thread Pool. Bu yuklab olish jarayonini
# alohida ishga tushirib, progress barga xalaqit bermaydi.
executor = ThreadPoolExecutor(max_workers=5)


# Yuklab olish jarayoni uchun maxsus hook
def download_progress_hook(d):
    """
    yt-dlp tomonidan chaqiriladigan hook. Progressni task_registry ga yozadi.
    """
    task_id = d.get('task_id')
    if not task_id or task_id not in task_registry:
        return

    # Yuklab olish holatini yangilash
    if d['status'] == 'downloading':
        # Yuklab olishning foizini hisoblash
        if d.get('fragment_count'):
            # Segmentlarga bo'lib yuklash progressi
            p = d.get('fragment_index', 0) / d.get('fragment_count') * 100
        elif d.get('total_bytes'):
            # Oddiy fayl yuklash progressi
            p = d.get('downloaded_bytes', 0) / d.get('total_bytes', 1) * 100
        else:
            p = task_registry[task_id]['progress']  # O'zgarmas holatda qoldirish

        task_registry[task_id]['progress'] = int(min(p, 99))  # 99% dan oshirmaymiz, chunki 100% tugashni anglatadi

        # Statusga tezlikni ham qo'shish (opsional)
        status_msg = "Yuklanmoqda..."
        if d.get('speed'):
            speed_mb = d['speed'] / 1048576 if d['speed'] else 0
            status_msg = f"Yuklanmoqda ({speed_mb:.2f} MiB/s)"

        task_registry[task_id]['status_message'] = status_msg

    elif d['status'] == 'finished':
        # Birlashgan (muxed) format ishlatilayotgani sababli, tugatish holati tezroq keladi.
        final_filepath = d['filename']
        task_registry[task_id]['file_info']['filepath'] = final_filepath
        task_registry[task_id]['file_info']['filename'] = os.path.basename(final_filepath)
        task_registry[task_id]['status'] = 'completed'
        task_registry[task_id]['progress'] = 100
        task_registry[task_id]['status_message'] = "✅ Bajarildi!"


def download_video_task(task_id, url):
    """
    Alohida thread'da ishlaydigan asosiy yuklab olish funksiyasi.
    """
    # Vazifaning dastlabki holatini o'rnatish
    task_registry[task_id] = {
        'status': 'processing',
        'progress': 0,
        'file_info': {'title': 'Noma\'lum video', 'filepath': None, 'filename': None},
        'error': None,
        'status_message': 'Vazifa boshlanmoqda...'
    }

    try:
        # **ENG TEZ VA BARQAROR YUKLASH UCHUN OPTIMIZATSIYA**
        ydl_opts = {
            # TEZLIK UCHUN YANGI QOIDA: Faqat audio va video birlashgan (muxed)
            # hamda maksimal 480p sifatdagi MP4/WEBM formatiga ustunlik berildi.
            # Bu fayl hajmini kamaytiradi va yuklashni tezlashtiradi.
            'format': 'best[ext=mp4][height<=480]/best[ext=webm][height<=480]/best',
            'outtmpl': os.path.join(DOWNLOAD_DIR, f'{task_id}_%(title)s.%(ext)s'),
            'progress_hooks': [download_progress_hook],
            'task_id': task_id,  # Hook uchun maxsus parametr
            'noplaylist': True,
            'retries': 15,  # Ulanishni tiklash uchun yuqori urinishlar soni
            'fragment_retries': 15,  # Parçalar uchun ham yuqori urinishlar soni
            'hls_use_mpegts': True,
            'concurrent_fragments': 12,  # Yuklash tezligini maksimal darajada oshirish uchun 12 ga ko'tarildi
            'noprogress': False,
            'no_warnings': True,
        }

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # Agar yuklab olish yakunlanmasa, shu yerda yakunlanadi (odatda 'finished' hook ishlaydi)
            if task_registry[task_id]['status'] != 'completed':
                task_registry[task_id]['file_info']['title'] = info.get('title', 'Yuklangan video')
                # Yakuniy fayl nomini topish
                final_filepath = ydl.prepare_filename(info)
                task_registry[task_id]['file_info']['filepath'] = final_filepath
                task_registry[task_id]['file_info']['filename'] = os.path.basename(final_filepath)
                task_registry[task_id]['status'] = 'completed'
                task_registry[task_id]['status_message'] = "✅ Bajarildi!"

    except DownloadError as e:
        task_registry[task_id]['status'] = 'failed'
        # Xato matnini tozalash va tushunarli qilish
        error_msg = str(e).split('\n')[-1].replace('ERROR: ', '').strip()
        task_registry[task_id][
            'error'] = f"Yuklab olishda xatolik yuz berdi: {error_msg}. Iltimos, havolani tekshirib ko'ring."
    except Exception as e:
        task_registry[task_id]['status'] = 'failed'
        task_registry[task_id]['error'] = f"Kutilmagan xato: {str(e)}"
    finally:
        # Agar xato bo'lsa va status yangilanmagan bo'lsa
        if task_registry[task_id]['status'] not in ['completed', 'failed']:
            task_registry[task_id]['status'] = 'failed'
            task_registry[task_id]['error'] = "Jarayon to'satdan to'xtatildi."


# --- Flask Route'lari ---

@app.route('/')
def index():
    """Asosiy sahifani ko'rsatish."""
    return render_template('index.html')


@app.route('/start_download', methods=['POST'])
def start_download():
    """Yuklab olish vazifasini boshlash va Task ID ni qaytarish."""
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({'success': False, 'error': 'URL kiritilmagan.'}), 400

    # Noyob Task ID yaratish
    task_id = str(uuid.uuid4())

    # Vazifani alohida Thread Pool da boshlash
    executor.submit(download_video_task, task_id, url)

    return jsonify({'success': True, 'task_id': task_id}), 202  # 202 Accepted


@app.route('/check_status/<task_id>', methods=['GET'])
def check_status(task_id):
    """Vazifaning joriy holatini tekshirish (Progress Bar uchun)."""
    task = task_registry.get(task_id)

    if not task:
        return jsonify({'status': 'unknown', 'error': 'Bunday vazifa topilmadi.'}), 404

    status = task['status']

    if status == 'completed':
        # Agar yakunlangan bo'lsa, to'liq ma'lumotni qaytarish
        file_info = task['file_info']

        # Brauzerga yuklab olish havolasini yaratish
        download_url = f'/downloads/{file_info["filename"]}'

        return jsonify({
            'status': status,
            'title': file_info['title'],
            'download_url': download_url,
            'filename': file_info['filename']
        })

    elif status == 'failed':
        return jsonify({'status': status, 'error': task['error']})

    else:
        # Agar jarayonda bo'lsa, faqat progressni qaytarish
        return jsonify({
            'status': status,
            'progress': task['progress'],
            'message': task['status_message']
        })


@app.route('/downloads/<filename>')
def serve_file(filename):
    """Yuklab olingan faylni uzatish uchun maxsus endpoint."""
    return send_from_directory(
        DOWNLOAD_DIR,
        filename,
        as_attachment=True
    )


# Ilovani ishga tushirish
if __name__ == '__main__':
    app.run(debug=True)
