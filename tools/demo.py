"""Fill a development database with a fictional demo organization (screenshots, trying it out).

    docker compose run --rm --no-deps -v ./tools:/app/tools:ro web python tools/demo.py

Every name is made up; nothing has sources, so nothing is probed. Refused when
ENVIRONMENT=prod. Prints the demo account's e-mail and a fresh random password.
Run it again to remove the previous demo organization first.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import uuid
from datetime import timedelta

from sqlalchemy import delete, select

from sentinel.accounts.passwords import Passwords, make_hasher
from sentinel.accounts.service import PRIVACY_NOTICE_VERSION
from sentinel.config import load_settings_or_exit
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import (
    AlertChannel,
    AlertRule,
    Change,
    Component,
    Dependency,
    Incident,
    IncidentKind,
    IncidentStatus,
    IncidentUpdate,
    Membership,
    Organization,
    Product,
    Risk,
    Service,
    StatusPage,
    User,
)

SLUG = "ornek-yazilim"
EMAIL = "demo@entegrator-status.invalid"
SERVICES = (  # (slug, name, kind, components)
    ("demo-entegrator", "Örnek Entegratör A.Ş.", "integrator", ("e-Fatura API", "Portal")),
    ("demo-ebelge", "Merkezi e-Belge Sistemi (örnek)", "authority", ("e-Fatura", "e-Arşiv")),
    ("demo-odeme", "Örnek Ödeme Kuruluşu", "saas_api", ("Ödeme API",)),
    ("demo-bulut", "Örnek Bulut", "cloud", ("Nesne Depolama", "DNS")),
    ("demo-sertifika", "Örnek Sertifika Hizmeti", "other", ("OCSP",)),
)


async def main() -> int:
    settings = load_settings_or_exit()
    if settings.environment == "prod":
        print("✗ refusing to write demo data with ENVIRONMENT=prod", file=sys.stderr)
        return 1
    password = secrets.token_urlsafe(12)
    now = utcnow()
    db = Database(settings.database_url.get_secret_value(), pool_size=1)
    try:
        async with db.system("demo: remove the previous demo organization") as s:
            await s.execute(delete(Organization).where(Organization.slug == SLUG))
            await s.execute(delete(Service).where(Service.slug.like("demo-%")))
            await s.execute(delete(User).where(User.email == EMAIL))
        async with db.system("demo: create a fictional organization") as s:
            user = User(
                email=EMAIL,
                display_name="Demo Kullanıcı",
                password_hash=Passwords(make_hasher(settings.environment)).hash(password),
                email_verified_at=now,
                privacy_notice_version=PRIVACY_NOTICE_VERSION,
                privacy_notice_seen_at=now,
            )
            org = Organization(slug=SLUG, name="Örnek Yazılım")
            s.add_all([user, org])
            await s.flush()
            s.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
            components: dict[str, uuid.UUID] = {}
            for slug, name, kind, parts in SERVICES:
                service = Service(slug=slug, name=name, kind=kind, tags=[])  # shared catalog
                s.add(service)
                await s.flush()
                for n, part in enumerate(parts):
                    component = Component(service_id=service.id, slug=f"c{n}", name=part, tags=[])
                    s.add(component)
                    await s.flush()
                    components[f"{slug}/{n}"] = component.id
            for origin, target in (
                ("demo-entegrator/0", "demo-ebelge/0"),
                ("demo-entegrator/0", "demo-ebelge/1"),
                ("demo-ebelge/0", "demo-sertifika/0"),
                ("demo-entegrator/1", "demo-bulut/1"),
            ):
                s.add(
                    Dependency(
                        from_component_id=components[origin],
                        to_component_id=components[target],
                        usage={},
                    )
                )
            products = {}
            for slug, name, uses in (
                ("fatura", "Fatura Modülü", ("demo-entegrator/0",)),
                ("odeme", "Online Tahsilat", ("demo-odeme/0", "demo-bulut/1")),
                ("arsiv", "Belge Arşivi", ("demo-bulut/0", "demo-entegrator/1")),
            ):
                product = Product(org_id=org.id, slug=slug, name=name)
                s.add(product)
                await s.flush()
                products[slug] = product.id
                for target in uses:
                    usage = {"operations": ["POST /invoices"]} if slug == "fatura" else {}
                    s.add(
                        Dependency(
                            org_id=org.id,
                            from_product_id=product.id,
                            to_component_id=components[target],
                            usage=usage,
                        )
                    )
            s.add(
                Incident(
                    kind=IncidentKind.AUTO,
                    status=IncidentStatus.OPEN,
                    component_id=components["demo-ebelge/1"],
                    started_at=now - timedelta(minutes=42),
                    cause="read_timeout",
                )
            )
            s.add(
                Change(
                    component_id=components["demo-entegrator/0"],
                    kind="contract",
                    category="breaking",
                    title="openapi.yaml",
                    detected_at=now - timedelta(hours=5),
                    fingerprint=uuid.uuid4().hex * 2,
                    detail={"operations": ["POST /invoices"]},
                )
            )
            s.add(
                Change(
                    component_id=components["demo-ebelge/0"],
                    kind="announcement",
                    category="package_update",
                    title="UBL-TR kod listesi güncellemesi",
                    detected_at=now - timedelta(days=1),
                    effective_at=now + timedelta(days=12),
                    fingerprint=uuid.uuid4().hex * 2,
                )
            )
            s.add(
                Risk(
                    component_id=components["demo-sertifika/0"],
                    kind="cert_expiry",
                    due_at=now + timedelta(days=9),
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            incident = Incident(
                org_id=org.id,
                kind=IncidentKind.MANUAL,
                status=IncidentStatus.IDENTIFIED,
                product_id=products["fatura"],
                title="Fatura gönderiminde gecikme",
                started_at=now - timedelta(minutes=30),
                created_by=user.id,
            )
            s.add(incident)
            await s.flush()
            s.add(
                IncidentUpdate(
                    org_id=org.id,
                    incident_id=incident.id,
                    status=IncidentStatus.IDENTIFIED,
                    body=(
                        "Entegratörün e-Arşiv servisindeki yavaşlık gönderimleri "
                        "geciktiriyor; kuyruk korunuyor, kayıp yok."
                    ),
                    author_id=user.id,
                )
            )
            s.add(
                StatusPage(
                    org_id=org.id,
                    title="Örnek Yazılım durum",
                    intro="Ürünlerimizin güncel durumu.",
                    enabled=True,
                    product_ids=[str(products["fatura"]), str(products["odeme"])],
                    show_dependencies=True,
                )
            )
            channel = AlertChannel(org_id=org.id, name="Operasyon e-postası", kind="email")
            s.add(channel)
            await s.flush()
            s.add(
                AlertRule(
                    org_id=org.id,
                    name="Yüksek önemli her şey",
                    channel_id=channel.id,
                    kinds=["incident", "change", "risk"],
                    min_severity="high",
                )
            )
        async with db.system("demo: check") as s:
            if await s.scalar(select(Organization.id).where(Organization.slug == SLUG)) is None:
                print("✗ the demo organization was not written", file=sys.stderr)
                return 1
    finally:
        await db.dispose()
    print(f"demo organization: /o/{SLUG}   public page: /s/{SLUG}")
    print(f"sign in as {EMAIL} with password {password}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
