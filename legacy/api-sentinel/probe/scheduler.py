"""APScheduler tabanlı zamanlayıcı — endpoint kontrollerini periyodik çalıştırır."""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.differ import compare_schemas
from core.fetcher import fetch_endpoint
from core.schema_extractor import extract_schema, extract_text_schema
from core.severity import SEVERITY_ORDER

logger = logging.getLogger(__name__)

# Varsayılan alert cooldown süresi (saniye) — aynı kanal/provider/endpoint için
DEFAULT_ALERT_COOLDOWN = 300  # 5 dakika


def _parse_interval(interval: str) -> dict[str, int]:
    """Interval string'ini APScheduler parametrelerine dönüştür.

    Args:
        interval: Interval string (örn: "5m", "1h", "30s")

    Returns:
        APScheduler interval parametreleri
    """
    match = re.match(r"^(\d+)([smh])$", interval)
    if not match:
        logger.warning(f"Geçersiz interval formatı: {interval}, varsayılan 5m kullanılıyor")
        return {"minutes": 5}

    value = int(match.group(1))
    unit = match.group(2)

    unit_map = {"s": "seconds", "m": "minutes", "h": "hours"}
    return {unit_map[unit]: value}


class MonitorScheduler:
    """API endpoint monitoring scheduler'ı.

    APScheduler AsyncIOScheduler kullanarak endpoint'leri periyodik kontrol eder.
    """

    def __init__(
        self,
        storage: Any,
        registry: Any,
        file_store: Any | None = None,
        alert_config: dict | None = None,
    ) -> None:
        """Scheduler başlat.

        Args:
            storage: Storage backend instance
            registry: Plugin registry instance
            file_store: Opsiyonel file store instance
            alert_config: Alert yapılandırması
        """
        self._scheduler = AsyncIOScheduler()
        self._storage = storage
        self._registry = registry
        self._file_store = file_store
        self._alert_config = alert_config or {}

    def start(self) -> None:
        """Scheduler'ı başlat."""
        self._scheduler.start()

        # Günlük retention temizliği job'ı ekle
        self._scheduler.add_job(
            self._run_retention_cleanup,
            trigger="interval",
            id="retention_cleanup",
            hours=6,
            replace_existing=True,
        )

        # Günlük özet rapor job'ı ekle
        self._scheduler.add_job(
            self._send_scheduled_report,
            trigger="interval",
            id="scheduled_report",
            hours=24,
            replace_existing=True,
        )

        logger.info("Scheduler başlatıldı (retention cleanup: her 6 saat, scheduled report: her 24 saat)")

    def shutdown(self) -> None:
        """Scheduler'ı durdur."""
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler durduruldu")

    def add_endpoint_job(
        self,
        provider_name: str,
        endpoint_name: str,
        interval: str = "5m",
    ) -> None:
        """Endpoint için periyodik kontrol job'ı ekle.

        Args:
            provider_name: Provider adı
            endpoint_name: Endpoint adı
            interval: Kontrol aralığı (örn: "5m")
        """
        job_id = f"{provider_name}_{endpoint_name}"

        # Mevcut job varsa kaldır
        existing_job = self._scheduler.get_job(job_id)
        if existing_job:
            self._scheduler.remove_job(job_id)

        interval_params = _parse_interval(interval)

        self._scheduler.add_job(
            self._check_endpoint,
            trigger="interval",
            id=job_id,
            args=[provider_name, endpoint_name],
            **interval_params,
            replace_existing=True,
        )

        logger.info(f"Scheduler job eklendi: {job_id} ({interval})")

    def remove_endpoint_job(self, provider_name: str, endpoint_name: str) -> None:
        """Endpoint job'ını kaldır.

        Args:
            provider_name: Provider adı
            endpoint_name: Endpoint adı
        """
        job_id = f"{provider_name}_{endpoint_name}"

        existing_job = self._scheduler.get_job(job_id)
        if existing_job:
            self._scheduler.remove_job(job_id)
            logger.info(f"Scheduler job kaldırıldı: {job_id}")

    async def _check_endpoint(self, provider_name: str, endpoint_name: str) -> None:
        """Tek bir endpoint'i kontrol et.

        Args:
            provider_name: Provider adı
            endpoint_name: Endpoint adı
        """
        logger.info(f"Endpoint kontrol ediliyor: {provider_name}/{endpoint_name}")

        try:
            # Provider bilgilerini al
            provider_data = await self._storage.get_provider(provider_name)
            if provider_data is None:
                logger.error(f"Provider bulunamadı: {provider_name}")
                return

            endpoints = await self._storage.get_endpoints(provider_name)
            ep_data = next((e for e in endpoints if e["name"] == endpoint_name), None)

            if ep_data is None:
                logger.error(f"Endpoint bulunamadı: {provider_name}/{endpoint_name}")
                return

            # Auth handler al
            auth_handler = None
            if provider_data.get("auth_type"):
                auth_cls = self._registry.get("auth_handler", provider_data["auth_type"])
                if auth_cls is not None:
                    auth_handler = auth_cls()

            # DB'den credential'ları al
            credentials = await self._storage.get_credentials(provider_name)

            # Auth config'i credential'larla birleştir
            auth_config = provider_data.get("auth_config") or {}
            auth_config["_credentials"] = credentials

            # Endpoint config oluştur
            endpoint_config = {
                "method": ep_data["method"],
                "base_url": provider_data["base_url"],
                "path": ep_data["path"],
                "path_params": {},
                "query_params": ep_data.get("query_params") or {},
                "test_payload": ep_data.get("test_payload"),
                "auth_config": auth_config,
            }

            # Fetch
            result = await fetch_endpoint(endpoint_config, auth_handler)

            # Endpoint durum bilgisini kaydet — sadece 2xx/3xx basarili sayilir
            _sc = result.get("status_code") or 0
            is_up = not result.get("error") and 200 <= _sc < 400
            await self._storage.upsert_endpoint_status(
                provider=provider_name,
                endpoint=endpoint_name,
                is_up=is_up,
                status_code=result.get("status_code"),
                response_time_ms=result.get("response_time_ms"),
                error_message=result.get("error"),
            )

            # Response time geçmişi kaydet (trend & uptime için)
            await self._storage.save_response_time(
                provider=provider_name,
                endpoint=endpoint_name,
                status_code=result.get("status_code"),
                response_time_ms=result.get("response_time_ms"),
                is_up=is_up,
            )

            if result.get("error"):
                logger.error(f"Fetch hatası: {provider_name}/{endpoint_name} — {result['error']}")
                return

            response_body = result.get("response_body")
            is_json = isinstance(response_body, (dict, list))

            # Schema cikar — JSON veya text
            if is_json:
                new_schema = extract_schema(response_body)
                response_sample = json.dumps(response_body)[:1000]
            else:
                # Non-JSON response (text, XML, Prometheus, HTML vb.)
                text_body = str(response_body) if response_body else ""
                content_type = (result.get("headers") or {}).get("content-type", "text/plain")
                new_schema = extract_text_schema(text_body, content_type)
                response_sample = text_body[:1000]

            # En son snapshot'ı al
            latest = await self._storage.get_latest_snapshot(provider_name, endpoint_name)

            if latest is None:
                # Ilk calisma — baseline kaydet
                await self._storage.save_snapshot(
                    provider=provider_name,
                    endpoint=endpoint_name,
                    schema_json=json.dumps(new_schema),
                    response_sample=response_sample,
                    status_code=result.get("status_code"),
                    response_time_ms=result.get("response_time_ms"),
                    version=1,
                )
                if self._file_store is not None:
                    self._file_store.save_snapshot(
                        provider_name, endpoint_name, new_schema, 1, response_body,
                    )
                logger.info(f"Baseline kaydedildi: {provider_name}/{endpoint_name}")
                return

            # Karsilastir
            old_schema = json.loads(latest["schema_json"])
            diff_result = compare_schemas(old_schema, new_schema, provider_name, endpoint_name)

            if not diff_result["has_changes"]:
                logger.debug(f"Degisiklik yok: {provider_name}/{endpoint_name}")
                return

            # Degisiklik var — kaydet ve alert gonder
            new_version = latest["version"] + 1

            await self._storage.save_snapshot(
                provider=provider_name,
                endpoint=endpoint_name,
                schema_json=json.dumps(new_schema),
                response_sample=response_sample,
                status_code=result.get("status_code"),
                response_time_ms=result.get("response_time_ms"),
                version=new_version,
            )

            await self._storage.save_change(
                provider=provider_name,
                endpoint=endpoint_name,
                diff_json=json.dumps(diff_result),
                max_severity=diff_result["max_severity"],
                old_version=latest["version"],
                new_version=new_version,
            )

            if self._file_store is not None:
                self._file_store.save_snapshot(
                    provider_name, endpoint_name, new_schema, new_version, response_body,
                )

            # Alert gönder
            await self._route_alerts(diff_result)

            logger.info(
                f"Değişiklik tespit edildi: {provider_name}/{endpoint_name} "
                f"v{latest['version']}→v{new_version} ({diff_result['max_severity']})"
            )

        except Exception as e:
            logger.error(f"Endpoint kontrol hatası: {provider_name}/{endpoint_name} — {e}")

    async def _route_alerts(self, diff_result: dict) -> None:
        """Aktif alert kanallarına severity'ye göre bildirim gönder.

        Cooldown mekanizması: Aynı kanal/provider/endpoint kombinasyonu için
        belirli süre içinde tekrar alert gönderilmez (varsayılan 5 dakika).

        Args:
            diff_result: Diff sonucu
        """
        max_severity = diff_result.get("max_severity", "INFO")
        channels_config = self._alert_config.get("channels", {})

        if not channels_config:
            logger.debug("Aktif alert kanalı yok, bildirim atlanıyor")
            return

        provider = diff_result.get("provider", "")
        endpoint = diff_result.get("endpoint", "")
        title = f"Schema değişikliği: {provider}/{endpoint}"
        message = diff_result.get("summary", "")

        # Cooldown süresini settings'ten al
        try:
            cooldown_seconds = int(
                await self._storage.get_setting("alert_cooldown_seconds", str(DEFAULT_ALERT_COOLDOWN))
            )
        except (ValueError, TypeError):
            cooldown_seconds = DEFAULT_ALERT_COOLDOWN

        for channel_name, channel_config in channels_config.items():
            # Kanal aktif mi kontrol et
            if not channel_config.get("enabled", False):
                continue

            # Min severity kontrolü
            min_severity = channel_config.get("min_severity", "INFO")
            if SEVERITY_ORDER.get(max_severity, 0) < SEVERITY_ORDER.get(min_severity, 0):
                continue

            # Cooldown kontrolü
            if cooldown_seconds > 0:
                last_alert_time = await self._storage.get_last_alert_time(
                    channel_name, provider, endpoint,
                )
                if last_alert_time:
                    try:
                        last_dt = datetime.fromisoformat(last_alert_time.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        elapsed = (now - last_dt).total_seconds()
                        if elapsed < cooldown_seconds:
                            logger.debug(
                                f"Alert cooldown aktif: {channel_name}/{provider}/{endpoint} "
                                f"({cooldown_seconds - elapsed:.0f}s kaldı)"
                            )
                            continue
                    except (ValueError, TypeError):
                        pass

            # Kanalı al ve gönder
            cls = self._registry.get("alert_channel", channel_name)
            if cls is None:
                logger.warning(f"Alert kanalı bulunamadı: {channel_name}")
                continue

            success = False
            error_msg: str | None = None

            try:
                instance = cls()
                success = await instance.send(
                    severity=max_severity,
                    title=title,
                    message=message,
                    details=diff_result,
                )
                if success:
                    logger.info(f"Alert gönderildi: {channel_name} ({max_severity})")
                else:
                    error_msg = "Kanal send() False döndürdü"
                    logger.warning(f"Alert gönderilemedi: {channel_name}")
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Alert gönderme hatası ({channel_name}): {e}")

            # Alert geçmişini kaydet
            try:
                await self._storage.save_alert_history(
                    channel_name=channel_name,
                    provider=provider,
                    endpoint=endpoint,
                    severity=max_severity,
                    title=title,
                    success=success,
                    error_message=error_msg,
                )
            except Exception as e:
                logger.error(f"Alert geçmişi kaydedilemedi: {e}")

    async def _run_retention_cleanup(self) -> None:
        """Retention politikasına göre eski verileri temizle.

        Settings'teki max_changes_retained ve max_snapshots_per_endpoint
        değerlerine göre eski snapshot ve change kayıtlarını siler.
        """
        try:
            max_changes = int(await self._storage.get_setting("max_changes_retained", "1000"))
            max_snapshots = int(await self._storage.get_setting("max_snapshots_per_endpoint", "50"))

            deleted_changes = await self._storage.cleanup_old_changes(max_changes)
            deleted_snapshots = await self._storage.cleanup_old_snapshots(max_snapshots)
            deleted_rt = await self._storage.cleanup_old_response_times(500)

            if deleted_changes > 0 or deleted_snapshots > 0 or deleted_rt > 0:
                await self._storage.vacuum()
                logger.info(
                    f"Retention temizliği: {deleted_changes} change, "
                    f"{deleted_snapshots} snapshot silindi"
                )
            else:
                logger.debug("Retention temizliği: silinecek kayıt yok")

        except Exception as e:
            logger.error(f"Retention temizliği hatası: {e}")

    async def _send_scheduled_report(self) -> None:
        """Günlük/haftalık özet rapor gönder.

        Settings'teki scheduled_report_enabled ve scheduled_report_interval
        değerlerine göre çalışır. Aktif alert kanallarına özet mesaj gönderir.
        """
        try:
            enabled = await self._storage.get_setting("scheduled_report_enabled", "false")
            if enabled.lower() != "true":
                logger.debug("Zamanlanmış rapor devre dışı, atlanıyor")
                return

            # Son 24 saatteki istatistikleri topla
            stats = await self._storage.get_stats()
            uptime_stats = await self._storage.get_all_uptime_stats(hours=24)
            endpoint_statuses = await self._storage.get_all_endpoint_statuses()

            up_count = sum(1 for s in endpoint_statuses if s.get("is_up"))
            down_count = sum(1 for s in endpoint_statuses if not s.get("is_up"))
            total_endpoints = up_count + down_count

            # Uptime ortalaması
            avg_uptime = 100.0
            if uptime_stats:
                avg_uptime = sum(u["uptime_pct"] for u in uptime_stats) / len(uptime_stats)

            # Rapor mesajı oluştur
            title = f"API Sentinel — Günlük Rapor ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
            lines = [
                f"Toplam Provider: {stats.get('provider_count', 0)}",
                f"Toplam Endpoint: {stats.get('endpoint_count', 0)}",
                f"Durum: {up_count} UP / {down_count} DOWN (toplam {total_endpoints})",
                f"Ortalama Uptime: %{avg_uptime:.1f}",
                f"Son 24 saatteki değişiklik: {stats.get('recent_changes_24h', 0)}",
                f"Onaylanmamış değişiklik: {stats.get('unacknowledged', 0)}",
            ]

            # DOWN olan endpoint'leri ekle
            down_endpoints = [s for s in endpoint_statuses if not s.get("is_up")]
            if down_endpoints:
                lines.append("")
                lines.append("DOWN Endpoint'ler:")
                for s in down_endpoints:
                    lines.append(f"  - {s['provider']}/{s['endpoint']}: {s.get('error_message', 'N/A')}")

            message = "\n".join(lines)

            # Aktif alert kanallarını DB'den oku
            alert_configs = await self._storage.get_alert_configs()
            sent_count = 0

            for ac in alert_configs:
                if not ac.get("enabled", False):
                    continue

                channel_name = ac["channel_name"]
                cls = self._registry.get("alert_channel", channel_name)
                if cls is None:
                    continue

                # Config'i şifre çözülmüş haliyle al
                full_config = await self._storage.get_alert_config(channel_name)
                config_data = full_config.get("config", {}) if full_config else {}

                try:
                    instance = cls()
                    success = await instance.send(
                        severity="INFO",
                        title=title,
                        message=message,
                        details={
                            "provider": "system",
                            "endpoint": "scheduled-report",
                            "summary": message,
                            "report_type": "daily",
                            "stats": stats,
                            "uptime_avg": avg_uptime,
                            "up_count": up_count,
                            "down_count": down_count,
                            "_config": config_data,
                        },
                    )
                    if success:
                        sent_count += 1
                        logger.info(f"Zamanlanmış rapor gönderildi: {channel_name}")
                    else:
                        logger.warning(f"Zamanlanmış rapor gönderilemedi: {channel_name}")
                except Exception as e:
                    logger.error(f"Zamanlanmış rapor gönderim hatası ({channel_name}): {e}")

            if sent_count == 0:
                logger.info("Zamanlanmış rapor: aktif kanal yok veya gönderilemedi")

        except Exception as e:
            logger.error(f"Zamanlanmış rapor hatası: {e}")

