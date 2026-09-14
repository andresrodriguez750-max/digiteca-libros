#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DigiTeca - Sincronizador en la Nube (GitHub Actions)
Descarga de Google Drive y sube a GitHub Releases a 1 Gbps desde los datacenters de GitHub.
Actualiza CockroachDB de forma segura. Tu PC puede estar apagada.
"""

import os
import sys
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import psycopg2
import gdown

sys.stdout.reconfigure(encoding='utf-8')

DB_URL = os.environ.get('DATABASE_URL')
GH_TOKEN = os.environ.get('RELEASE_TOKEN') or os.environ.get('GITHUB_TOKEN')
GH_OWNER = "andresrodriguez750-max"
GH_REPO = "digiteca-libros"

GH_API = "https://api.github.com"
GH_HEADERS = {
    'Authorization': f'Bearer {GH_TOKEN}',
    'Accept': 'application/vnd.github+json'
}

from requests.adapters import HTTPAdapter
session = requests.Session()
adapter = HTTPAdapter(pool_connections=40, pool_maxsize=40, max_retries=3)
session.mount('https://', adapter)
session.mount('http://', adapter)

lock = threading.Lock()
RELEASES_CACHE = {}


def get_db():
    url = DB_URL
    if 'sslmode=verify-full' in url:
        url = url.replace('sslmode=verify-full', 'sslmode=require')
    elif 'sslmode' not in url:
        url += '?sslmode=require' if '?' not in url else '&sslmode=require'
    return psycopg2.connect(url)


def get_or_create_release(tag_name="libros-v1", title="Biblioteca DigiTeca - Lote 1"):
    with lock:
        if tag_name in RELEASES_CACHE:
            return RELEASES_CACHE[tag_name]

        url = f"{GH_API}/repos/{GH_OWNER}/{GH_REPO}/releases/tags/{tag_name}"
        r = session.get(url, headers=GH_HEADERS)
        if r.status_code == 200:
            data = r.json()
            upload_url = data['upload_url'].split('{')[0]
            existing_assets = {a['name']: a['browser_download_url'] for a in data.get('assets', [])}
            RELEASES_CACHE[tag_name] = (data['id'], upload_url, existing_assets)
            return RELEASES_CACHE[tag_name]

        create_url = f"{GH_API}/repos/{GH_OWNER}/{GH_REPO}/releases"
        payload = {
            'tag_name': tag_name,
            'name': title,
            'body': f'Colección pública de libros digitales educativos de DigiTeca ({tag_name})'
        }
        cr = session.post(create_url, headers=GH_HEADERS, json=payload)
        if cr.status_code in (200, 201):
            data = cr.json()
            upload_url = data['upload_url'].split('{')[0]
            RELEASES_CACHE[tag_name] = (data['id'], upload_url, {})
            return RELEASES_CACHE[tag_name]

        raise Exception(f"Error release: {cr.status_code} {cr.text}")


def sanitize_filename(title, book_id):
    clean = re.sub(r'[^a-zA-Z0-9_\-]', '_', title.lower())
    clean = re.sub(r'_+', '_', clean).strip('_')
    return f"libro_{book_id}_{clean[:35]}.pdf"


def download_drive(drive_id, dest_path):
    try:
        url = f"https://drive.google.com/uc?id={drive_id}&export=download"
        s = requests.Session()
        resp = s.get(url, stream=True, timeout=30)
        for k, v in resp.cookies.items():
            if k.startswith('download_warning'):
                resp = s.get(f"{url}&confirm={v}", stream=True, timeout=30)
                break

        content_type = resp.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type and len(resp.content) < 50000:
            try:
                gdown.download(id=drive_id, output=dest_path, quiet=True)
            except Exception:
                pass
        else:
            with open(dest_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
    except Exception:
        try:
            gdown.download(id=drive_id, output=dest_path, quiet=True)
        except Exception:
            pass

    return os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000


def upload_pdf(upload_url, filename, filepath):
    file_size = os.path.getsize(filepath)
    headers = {
        'Authorization': f'Bearer {GH_TOKEN}',
        'Content-Type': 'application/pdf',
        'Content-Length': str(file_size)
    }
    try:
        with open(filepath, 'rb') as f:
            r = session.post(f"{upload_url}?name={filename}", headers=headers, data=f, timeout=120)
        if r.status_code in (200, 201):
            return r.json().get('browser_download_url')
        elif r.status_code == 422 and 'already_exists' in r.text:
            return f"https://github.com/{GH_OWNER}/{GH_REPO}/releases/download/libros-v1/{filename}"
    except Exception:
        pass
    return None


def sync_book(book, upload_url, existing_assets):
    book_id, title, pdf_url = book
    safe_name = sanitize_filename(title, book_id)

    with lock:
        already = existing_assets.get(safe_name)

    if already:
        return True, book_id, already, f"Ya en GitHub: {title[:25]}"

    m = re.search(r'/d/([a-zA-Z0-9_-]{25,})', pdf_url) or re.search(r'id=([a-zA-Z0-9_-]{25,})', pdf_url)
    if not m:
        return False, book_id, None, f"Sin ID Drive: {title[:25]}"

    drive_id = m.group(1)
    temp_path = f"/tmp/{safe_name}"

    ok = download_drive(drive_id, temp_path)
    if not ok:
        return False, book_id, None, f"Drive inaccesible: {title[:25]}"

    public_url = upload_pdf(upload_url, safe_name, temp_path)

    try:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    except Exception:
        pass

    if public_url:
        with lock:
            existing_assets[safe_name] = public_url
        return True, book_id, public_url, f"✅ Subido: {title[:30]}"
    else:
        return False, book_id, None, f"Error subida: {title[:25]}"


def main():
    print("==========================================================")
    print("⚡ DIGITECA - CLOUD-TO-CLOUD SYNC (GITHUB ACTIONS 1 Gbps)")
    print("==========================================================")

    if not DB_URL or not GH_TOKEN:
        print("❌ Error: Faltan variables de entorno DATABASE_URL o RELEASE_TOKEN")
        sys.exit(1)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, titulo, pdf_url FROM libros WHERE pdf_url LIKE '%drive.google.com%' ORDER BY id ASC")
    books = cur.fetchall()
    cur.close()
    conn.close()

    total = len(books)
    print(f"📚 Libros pendientes en Drive: {total}")

    if total == 0:
        print("🎉 ¡Todos los libros ya están en GitHub Releases!")
        return

    rel_id, upload_url, existing_assets = get_or_create_release("libros-v1")

    # Usar 10 hilos en el runner de GitHub
    print("🚀 Iniciando transferencia paralela de alta velocidad (10 hilos)...")
    batch_updates = []
    success_count = 0
    error_count = 0

    conn = get_db()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(sync_book, b, upload_url, existing_assets): b for b in books}
        for fut in as_completed(futures):
            ok, b_id, pub_url, msg = fut.result()
            print(msg)
            if ok and pub_url:
                success_count += 1
                batch_updates.append((pub_url, b_id))
            else:
                error_count += 1

            # Actualizar la BD en lotes de 25
            if len(batch_updates) >= 25:
                with conn.cursor() as c:
                    for p_url, book_id in batch_updates:
                        c.execute("UPDATE libros SET pdf_url = %s WHERE id = %s", (p_url, book_id))
                    conn.commit()
                batch_updates = []

    # Actualizar remanentes
    if batch_updates:
        with conn.cursor() as c:
            for p_url, book_id in batch_updates:
                c.execute("UPDATE libros SET pdf_url = %s WHERE id = %s", (p_url, book_id))
            conn.commit()

    conn.close()
    print("==========================================================")
    print(f"🎉 Sincronización en la Nube completada: {success_count} exitosos | {error_count} errores")
    print("==========================================================")


if __name__ == '__main__':
    main()
