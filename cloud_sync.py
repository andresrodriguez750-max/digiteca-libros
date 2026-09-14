#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import psycopg2
import gdown
import tempfile
from requests.adapters import HTTPAdapter

sys.stdout.reconfigure(encoding='utf-8')

DB_URL = os.environ.get('DATABASE_URL')
GH_TOKEN = os.environ.get('RELEASE_TOKEN') or os.environ.get('GITHUB_TOKEN')
GH_OWNER = 'andresrodriguez750-max'
GH_REPO = 'digiteca-libros'

GH_API = 'https://api.github.com'
GH_HEADERS = {
    'Authorization': f'Bearer {GH_TOKEN}',
    'Accept': 'application/vnd.github+json'
}

session = requests.Session()
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=3)
session.mount('https://', adapter)
session.mount('http://', adapter)


def get_db():
    url = DB_URL
    if 'sslmode=verify-full' in url:
        url = url.replace('sslmode=verify-full', 'sslmode=require')
    elif 'sslmode' not in url:
        url += '?sslmode=require' if '?' not in url else '&sslmode=require'
    return psycopg2.connect(url)


class ReleaseManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.current_volume = 1
        self.volumes = {}
        self.init_volumes()

    def init_volumes(self):
        r = session.get(f'{GH_API}/repos/{GH_OWNER}/{GH_REPO}/releases', headers=GH_HEADERS)
        if r.status_code == 200:
            for rel in r.json():
                tag = rel.get('tag_name', '')
                if tag.startswith('libros-v'):
                    try:
                        v_num = int(tag.replace('libros-v', ''))
                    except ValueError:
                        continue
                    upload_url = rel['upload_url'].split('{')[0]
                    assets = {a['name']: a['browser_download_url'] for a in rel.get('assets', [])}
                    self.volumes[v_num] = {
                        'id': rel['id'],
                        'upload_url': upload_url,
                        'assets': assets,
                        'tag': tag
                    }

        v = 1
        while v in self.volumes and len(self.volumes[v]['assets']) >= 990:
            v += 1
        self.current_volume = v
        self.get_or_create_volume(self.current_volume)
        print(f'Volumen de destino activo: libros-v{self.current_volume}')

    def get_or_create_volume(self, v_num):
        with self.lock:
            if v_num in self.volumes:
                return self.volumes[v_num]
            tag = f'libros-v{v_num}'
            payload = {
                'tag_name': tag,
                'name': f'Biblioteca DigiTeca - Lote {v_num}',
                'body': f'Coleccion publica de libros digitales educativos de DigiTeca ({tag})'
            }
            cr = session.post(f'{GH_API}/repos/{GH_OWNER}/{GH_REPO}/releases', headers=GH_HEADERS, json=payload)
            if cr.status_code in (200, 201):
                data = cr.json()
                upload_url = data['upload_url'].split('{')[0]
                self.volumes[v_num] = {
                    'id': data['id'],
                    'upload_url': upload_url,
                    'assets': {},
                    'tag': tag
                }
                print(f'Creado nuevo volumen: {tag}')
                return self.volumes[v_num]
            raise Exception(f'Fallo al crear release {tag}: {cr.status_code} {cr.text}')

    def get_upload_target(self):
        with self.lock:
            vol = self.volumes.get(self.current_volume)
            if not vol or len(vol['assets']) >= 990:
                self.current_volume += 1
                vol = self.get_or_create_volume(self.current_volume)
            return self.current_volume, vol['upload_url'], vol['tag']

    def register_asset(self, vol_num, name, download_url):
        with self.lock:
            if vol_num in self.volumes:
                self.volumes[vol_num]['assets'][name] = download_url
                if len(self.volumes[vol_num]['assets']) >= 990:
                    self.current_volume = max(self.current_volume, vol_num + 1)
                    self.get_or_create_volume(self.current_volume)

    def find_existing(self, safe_name):
        with self.lock:
            for vol in self.volumes.values():
                if safe_name in vol['assets']:
                    return vol['assets'][safe_name]
        return None


rel_mgr = None


def sanitize_filename(title, book_id):
    clean = re.sub(r'[^a-zA-Z0-9_\-]', '_', title.lower())
    clean = re.sub(r'_+', '_', clean).strip('_')
    return f'libro_{book_id}_{clean[:35]}.pdf'


def download_drive(drive_id, dest_path):
    try:
        url1 = f'https://drive.usercontent.google.com/download?id={drive_id}&export=download&confirm=t'
        s = requests.Session()
        resp = s.get(url1, stream=True, timeout=45)
        ct = resp.headers.get('Content-Type', '').lower()
        if resp.status_code == 200 and ('pdf' in ct or 'octet-stream' in ct):
            with open(dest_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000:
                return True
    except Exception:
        pass

    try:
        url = f'https://drive.google.com/uc?id={drive_id}&export=download'
        s = requests.Session()
        resp = s.get(url, stream=True, timeout=30)
        for k, v in resp.cookies.items():
            if k.startswith('download_warning'):
                resp = s.get(f'{url}&confirm={v}', stream=True, timeout=30)
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


def upload_pdf(filename, filepath):
    global rel_mgr
    file_size = os.path.getsize(filepath)

    for attempt in range(3):
        vol_num, upload_url, tag_name = rel_mgr.get_upload_target()
        headers = {
            'Authorization': f'Bearer {GH_TOKEN}',
            'Content-Type': 'application/pdf',
            'Content-Length': str(file_size)
        }
        try:
            with open(filepath, 'rb') as f:
                r = session.post(f'{upload_url}?name={filename}', headers=headers, data=f, timeout=120)
            if r.status_code in (200, 201):
                dl_url = r.json().get('browser_download_url')
                rel_mgr.register_asset(vol_num, filename, dl_url)
                return dl_url
            elif r.status_code == 422:
                if 'file_count' in r.text or 'limited to 1000' in r.text:
                    print(f'{tag_name} lleno (1000 archivos). Rotando a siguiente volumen...')
                    with rel_mgr.lock:
                        rel_mgr.current_volume = max(rel_mgr.current_volume, vol_num + 1)
                        rel_mgr.get_or_create_volume(rel_mgr.current_volume)
                    continue
                elif 'already_exists' in r.text:
                    dl_url = f'https://github.com/{GH_OWNER}/{GH_REPO}/releases/download/{tag_name}/{filename}'
                    rel_mgr.register_asset(vol_num, filename, dl_url)
                    return dl_url
        except Exception:
            time.sleep(2)

    return None


def sync_book(book):
    global rel_mgr
    book_id, title, pdf_url = book
    safe_name = sanitize_filename(title, book_id)

    already = rel_mgr.find_existing(safe_name)
    if already:
        return True, book_id, already, f'Ya en GitHub: {title[:25]}'

    m = re.search(r'/d/([a-zA-Z0-9_-]{25,})', pdf_url) or re.search(r'id=([a-zA-Z0-9_-]{25,})', pdf_url)
    if not m:
        return False, book_id, None, f'Sin ID Drive: {title[:25]}'

    drive_id = m.group(1)
    temp_path = os.path.join(tempfile.gettempdir(), safe_name)

    ok = download_drive(drive_id, temp_path)
    if not ok:
        return False, book_id, None, f'Drive inaccesible: {title[:25]}'

    public_url = upload_pdf(safe_name, temp_path)

    try:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    except Exception:
        pass

    if public_url:
        return True, book_id, public_url, f'Subido: {title[:30]}'
    else:
        return False, book_id, None, f'Error subida: {title[:25]}'


def main():
    global rel_mgr
    print('==========================================================')
    print('DIGITECA - CLOUD-TO-CLOUD SYNC (GITHUB ACTIONS 1 Gbps)')
    print('==========================================================')

    if not DB_URL or not GH_TOKEN:
        print('Error: Faltan variables de entorno DATABASE_URL o RELEASE_TOKEN')
        sys.exit(1)

    print('Conectando a la base de datos CockroachDB...')
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, titulo, pdf_url FROM libros WHERE pdf_url LIKE '%drive.google.com%' ORDER BY id ASC")
    books = cur.fetchall()
    cur.close()
    conn.close()

    total = len(books)
    print(f'Libros pendientes en Drive: {total}')

    if total == 0:
        print('Todos los libros ya estan en GitHub Releases!')
        return

    print('Inicializando gestor de volumenes de GitHub Releases...')
    rel_mgr = ReleaseManager()

    print('Iniciando transferencia paralela de alta velocidad (10 hilos)...')
    batch_updates = []
    success_count = 0
    error_count = 0

    conn = get_db()

    processed = 0
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(sync_book, b): b for b in books}
        for fut in as_completed(futures):
            processed += 1
            ok, b_id, pub_url, msg = fut.result()
            pct = (processed / total) * 100
            print(f'[{processed}/{total}] ({pct:.1f}%) {msg}')
            if ok and pub_url:
                success_count += 1
                batch_updates.append((pub_url, b_id))
            else:
                error_count += 1

            if len(batch_updates) >= 10:
                try:
                    with conn.cursor() as c:
                        for p_url, book_id in batch_updates:
                            c.execute('UPDATE libros SET pdf_url = %s WHERE id = %s', (p_url, book_id))
                        conn.commit()
                    batch_updates = []
                except Exception as e:
                    print(f'Error guardando lote en BD: {e}')
                    try:
                        conn = get_db()
                    except Exception:
                        pass

    if batch_updates:
        try:
            with conn.cursor() as c:
                for p_url, book_id in batch_updates:
                    c.execute('UPDATE libros SET pdf_url = %s WHERE id = %s', (p_url, book_id))
                conn.commit()
        except Exception as e:
            print(f'Error final guardando lote en BD: {e}')

    conn.close()
    print('==========================================================')
    print(f'Sincronizacion completada: {success_count} exitosos | {error_count} errores')
    print('==========================================================')


if __name__ == '__main__':
    main()
