"""Changelog sayfalarını izleyip hash karşılaştırması yapan ana modül."""

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from notify import send_notification

logger = logging.getLogger(__name__)

# Veritabanı dosya yolu
DB_PATH: str = "/app/data/changelog.db"


def _get_db_path() -> str:
    """Veritabanı dosya yolunu döndür.

    Returns:
        Veritabanı dosya yolu
    """
    return DB_PATH


def init_db(db_path: str | None = None) -> None:
    """SQLite veritabanını ve gerekli tabloları oluştur.

    Args:
        db_path: Veritabanı dosya yolu (None ise varsayılan kullanılır)
    """
    path = db_path or _get_db_path()

    # Dizin yoksa oluştur
    db_dir = Path(path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    cursor = conn.cursor()

    # Hash tablosu oluştur
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS page_hashes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            hash TEXT NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)

    # İsme göre hızlı sorgu için indeks
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_page_hashes_name
        ON page_hashes (name)
    """)

    conn.commit()
    conn.close()

    logger.info(f"Veritabanı başlatıldı: {path}")


def _get_stored_hash(name: str, db_path: str | None = None) -> str | None:
    """Veritabanından en son kaydedilmiş hash değerini getir.

    Args:
        name: Hedef adı
        db_path: Veritabanı dosya yolu (None ise varsayılan kullanılır)

    Returns:
        En son hash değeri veya None (ilk çalışmada)
    """
    path = db_path or _get_db_path()
    conn = sqlite3.connect(path)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT hash FROM page_hashes WHERE name = ? ORDER BY id DESC LIMIT 1",
        (name,),
    )

    row = cursor.fetchone()
    conn.close()

    if row is not None:
        return row[0]

    return None


def _store_hash(name: str, url: str, content_hash: str, db_path: str | None = None) -> None:
    """Yeni hash değerini veritabanına kaydet.

    Args:
        name: Hedef adı
        url: Sayfa URL'si
        content_hash: Hesaplanan SHA256 hash
        db_path: Veritabanı dosya yolu (None ise varsayılan kullanılır)
    """
    path = db_path or _get_db_path()
    conn = sqlite3.connect(path)
    cursor = conn.cursor()

    checked_at = datetime.now(timezone.utc).isoformat()

    cursor.execute(
        "INSERT INTO page_hashes (name, url, hash, checked_at) VALUES (?, ?, ?, ?)",
        (name, url, content_hash, checked_at),
    )

    conn.commit()
    conn.close()

    logger.debug(f"Hash kaydedildi: {name} -> {content_hash[:16]}...")


async def fetch_page(url: str, timeout: float = 30.0) -> str:
    """Belirtilen URL'den sayfa içeriğini indir.

    Args:
        url: İndirilecek sayfa URL'si
        timeout: HTTP istek zaman aşımı (saniye)

    Returns:
        Sayfa HTML içeriği

    Raises:
        httpx.HTTPStatusError: HTTP durum kodu hata döndüğünde
        httpx.RequestError: Bağlantı hatası oluştuğunda
    """
    headers = {
        "User-Agent": "API-Sentinel-ChangelogWatcher/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
    ) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()

        logger.debug(f"Sayfa indirildi: {url} (status={response.status_code})")
        return response.text


def parse_content(html: str, selector: str) -> str:
    """HTML içeriğinden CSS seçici ile hedef bölümü çıkar.

    Args:
        html: Ham HTML içeriği
        selector: CSS seçici (ör: ".changelog-content")

    Returns:
        Seçilen bölümün metin içeriği (bulunamazsa tüm body metni)
    """
    soup = BeautifulSoup(html, "html.parser")

    # CSS seçici ile hedef elementi bul
    elements = soup.select(selector)

    if elements:
        # Tüm eşleşen elementlerin metnini birleştir
        combined_text = "\n".join(element.get_text(strip=True) for element in elements)
        logger.debug(f"Seçici '{selector}' ile {len(elements)} element bulundu")
        return combined_text

    # Seçici ile element bulunamazsa body metnini kullan
    body = soup.find("body")
    if body is not None:
        logger.warning(f"Seçici '{selector}' ile element bulunamadı, body kullanılıyor")
        return body.get_text(strip=True)

    # Body da yoksa tüm metin
    logger.warning("Body elementi bulunamadı, tüm sayfa metni kullanılıyor")
    return soup.get_text(strip=True)


def compute_hash(content: str) -> str:
    """İçerik için SHA256 hash hesapla.

    Args:
        content: Hash'lenecek metin içeriği

    Returns:
        SHA256 hash değeri (hex string)
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def check_target(target: dict[str, Any], db_path: str | None = None) -> dict[str, Any]:
    """Tek bir hedefi kontrol et: sayfa indir, parse et, hash karşılaştır.

    Args:
        target: Hedef yapılandırması (name, url, selector, notify_channels)
        db_path: Veritabanı dosya yolu (None ise varsayılan kullanılır)

    Returns:
        Kontrol sonucu dict'i (name, url, changed, hash, error vb.)
    """
    name: str = target["name"]
    url: str = target["url"]
    selector: str = target.get("selector", "body")
    notify_channels: list[str] = target.get("notify_channels", [])

    result: dict[str, Any] = {
        "name": name,
        "url": url,
        "changed": False,
        "hash": None,
        "previous_hash": None,
        "error": None,
    }

    try:
        # Sayfayı indir
        html = await fetch_page(url)

        # İçeriği parse et
        content = parse_content(html, selector)

        # Hash hesapla
        current_hash = compute_hash(content)
        result["hash"] = current_hash

        # Önceki hash'i al
        previous_hash = _get_stored_hash(name, db_path)
        result["previous_hash"] = previous_hash

        if previous_hash is None:
            # İlk çalışma — baseline kaydet
            _store_hash(name, url, current_hash, db_path)
            logger.info(f"Baseline kaydedildi: {name} (hash={current_hash[:16]}...)")
            return result

        if current_hash != previous_hash:
            # Değişiklik tespit edildi
            result["changed"] = True
            _store_hash(name, url, current_hash, db_path)

            logger.warning(
                f"Değişiklik tespit edildi: {name} "
                f"(eski={previous_hash[:16]}... -> yeni={current_hash[:16]}...)"
            )

            # Bildirim gönder
            await send_notification(
                target_name=name,
                url=url,
                old_hash=previous_hash,
                new_hash=current_hash,
                channels=notify_channels,
            )
        else:
            # Değişiklik yok
            logger.debug(f"Değişiklik yok: {name}")

    except httpx.HTTPStatusError as exc:
        error_msg = f"HTTP hatası: {exc.response.status_code} — {url}"
        result["error"] = error_msg
        logger.error(f"{name}: {error_msg}")

    except httpx.RequestError as exc:
        error_msg = f"Bağlantı hatası: {exc} — {url}"
        result["error"] = error_msg
        logger.error(f"{name}: {error_msg}")

    except sqlite3.Error as exc:
        error_msg = f"Veritabanı hatası: {exc}"
        result["error"] = error_msg
        logger.error(f"{name}: {error_msg}")

    return result
