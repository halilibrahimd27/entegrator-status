"""Organizations, members, roles, invitations, API tokens and the audit log."""

from __future__ import annotations

from datetime import timedelta

import httpx2
from fastapi import FastAPI
from sqlalchemy import select, update

from org_support import browser, create_org, invite, join, new_token
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.models import ApiToken, Invitation, Membership, User
from sentinel.web.services import AppServices
from support import link_in, post_form, signed_in

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"


async def membership_id(services: AppServices, email: str) -> str:
    async with services.db.system("test lookup") as s:
        found = await s.scalar(
            select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(User.email == email)
        )
    return str(found)


async def test_create_list_and_rename(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    assert await create_org(http, "Çağrı Şirketi A.Ş.") == "cagri-sirketi-a-s"
    assert await create_org(http, "Çağrı Şirketi A.Ş.") == "cagri-sirketi-a-s-2"
    listing = await http.get("/orglar")
    assert listing.text.count("Çağrı Şirketi A.Ş.") == 2
    assert "Sahip" in listing.text

    renamed = await post_form(http, "/o/cagri-sirketi-a-s/ayarlar", {"name": "Yeni Ad"})
    assert renamed.status_code == 303
    assert "Yeni Ad" in (await http.get("/o/cagri-sirketi-a-s")).text
    audit = await http.get("/o/cagri-sirketi-a-s/denetim")
    assert "org.created" in audit.text
    assert "org.renamed" in audit.text

    bad = await post_form(http, "/orglar", {"name": "x"}, csrf_from="/orglar")
    assert bad.status_code == 400
    assert "2 ile 120 karakter" in bad.text


async def test_invitation_flow(app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail]) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    async with join(app, outbox, http, slug, BOB) as bob:
        listing = await bob.get("/orglar")
        assert "Alpha" in listing.text
        assert "Üye" in listing.text
        assert (await bob.get(f"/o/{slug}")).status_code == 200
        assert (await bob.get(f"/o/{slug}/ayarlar")).status_code == 200
        # A member cannot manage tokens or read the audit log.
        assert (await bob.get(f"/o/{slug}/tokenlar")).status_code == 403
        assert (await bob.get(f"/o/{slug}/denetim")).status_code == 403
    members = await http.get(f"/o/{slug}/uyeler")
    assert BOB in members.text
    assert "member.joined" in (await http.get(f"/o/{slug}/denetim")).text


async def test_invitation_is_bound_to_its_address(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    assert (await invite(http, slug, "someone-else@example.invalid")).status_code == 303
    path = link_in(outbox[-1], "davet")
    async with browser(app) as bob:
        await bob.get(path)
        await signed_in(bob, outbox, BOB)
        refused = await post_form(bob, "/davet/kabul", {}, csrf_from="/orglar")
        assert refused.status_code == 400
        assert "başka bir e-posta adresine" in refused.text
        assert (await bob.get(f"/o/{slug}")).status_code == 404


async def test_revoked_and_expired_invitations(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    await invite(http, slug, BOB)
    revoked_path = link_in(outbox[-1], "davet")
    async with services.db.system("test lookup") as s:
        invitation_id = await s.scalar(select(Invitation.id))
    done = await post_form(
        http, f"/o/{slug}/davetler/{invitation_id}/iptal", {}, csrf_from=f"/o/{slug}/uyeler"
    )
    assert done.status_code == 303
    assert (await http.get(revoked_path)).status_code == 400

    await invite(http, slug, BOB)
    expired_path = link_in(outbox[-1], "davet")
    async with services.db.system("test change") as s:
        await s.execute(update(Invitation).values(expires_at=utcnow() - timedelta(seconds=1)))
    assert (await http.get(expired_path)).status_code == 400


async def test_cannot_invite_an_existing_member(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    response = await invite(http, slug, ALICE)
    assert response.status_code == 400
    assert "zaten üye" in response.text


async def test_roles_follow_the_grant_rules(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    alice_m = await membership_id(services, ALICE)
    async with join(app, outbox, http, slug, BOB, role="admin") as bob:
        members = f"/o/{slug}/uyeler"
        # An admin cannot touch an owner, nor hand out ownership.
        demote = await post_form(
            bob, f"{members}/{alice_m}/rol", {"role": "viewer"}, csrf_from=members
        )
        assert demote.status_code == 403
        assert (
            await post_form(bob, f"{members}/{alice_m}/cikar", {}, csrf_from=members)
        ).status_code == 403
        assert (await invite(bob, slug, "carol@example.invalid", role="owner")).status_code == 403
        assert (await invite(bob, slug, "carol@example.invalid", role="viewer")).status_code == 303

    # The last owner can neither be demoted nor leave.
    last = await post_form(
        http, f"/o/{slug}/uyeler/{alice_m}/rol", {"role": "admin"}, csrf_from=f"/o/{slug}/uyeler"
    )
    assert last.status_code == 400
    assert "en az bir sahip" in last.text
    leave = await post_form(http, f"/o/{slug}/ayril", {}, csrf_from=f"/o/{slug}/ayarlar")
    assert leave.status_code == 400

    bob_m = await membership_id(services, BOB)
    promote = await post_form(
        http, f"/o/{slug}/uyeler/{bob_m}/rol", {"role": "owner"}, csrf_from=f"/o/{slug}/uyeler"
    )
    assert promote.status_code == 303
    # With a second owner, the first may step down.
    step_down = await post_form(
        http, f"/o/{slug}/uyeler/{alice_m}/rol", {"role": "member"}, csrf_from=f"/o/{slug}/uyeler"
    )
    assert step_down.status_code == 303


async def test_removed_member_loses_access(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    async with join(app, outbox, http, slug, BOB) as bob:
        bob_m = await membership_id(services, BOB)
        removed = await post_form(
            http, f"/o/{slug}/uyeler/{bob_m}/cikar", {}, csrf_from=f"/o/{slug}/uyeler"
        )
        assert removed.status_code == 303
        assert (await bob.get(f"/o/{slug}")).status_code == 404
        assert "Alpha" not in (await bob.get("/orglar")).text


async def test_member_can_leave(app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail]) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    async with join(app, outbox, http, slug, BOB) as bob:
        left = await post_form(bob, f"/o/{slug}/ayril", {}, csrf_from=f"/o/{slug}/ayarlar")
        assert left.headers["location"] == "/orglar"
        assert (await bob.get(f"/o/{slug}")).status_code == 404


async def test_viewer_is_read_only(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    async with join(app, outbox, http, slug, BOB, role="viewer") as bob:
        rename = await post_form(bob, f"/o/{slug}/ayarlar", {"name": "Bob'un adı"})
        assert rename.status_code == 403
        assert (await invite(bob, slug, "carol@example.invalid")).status_code == 403
        members = await bob.get(f"/o/{slug}/uyeler")
        assert members.status_code == 200
        assert "/uyeler/davet" not in members.text  # controls are hidden too


async def test_api_tokens(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    raw = await new_token(http, slug)

    listing = await http.get(f"/o/{slug}/tokenlar")
    assert raw not in listing.text  # shown once only
    assert raw[:11] in listing.text  # the prefix is listed

    async with browser(app) as api:
        ok = await api.get("/api/v1/org", headers={"Authorization": f"Bearer {raw}"})
        assert ok.json() == {"slug": slug, "name": "Alpha"}
        for bad in ("", "Bearer", "Bearer es_zzzzzzzz_x", f"Bearer {raw}x", f"Basic {raw}"):
            response = await api.get("/api/v1/org", headers={"Authorization": bad} if bad else {})
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"

    # Cookies never authenticate the API.
    assert (await http.get("/api/v1/org")).status_code == 401

    async with services.db.system("test lookup") as s:
        token_id = await s.scalar(select(ApiToken.id))
    revoked = await post_form(
        http, f"/o/{slug}/tokenlar/{token_id}/iptal", {}, csrf_from=f"/o/{slug}/tokenlar"
    )
    assert revoked.status_code == 303
    async with browser(app) as api:
        assert (
            await api.get("/api/v1/org", headers={"Authorization": f"Bearer {raw}"})
        ).status_code == 401

    expiring = await new_token(http, slug, name="kısa ömürlü")
    async with services.db.system("test change") as s:
        await s.execute(update(ApiToken).values(expires_at=utcnow() - timedelta(seconds=1)))
    async with browser(app) as api:
        assert (
            await api.get("/api/v1/org", headers={"Authorization": f"Bearer {expiring}"})
        ).status_code == 401


async def test_platform_admin_without_totp_is_sent_to_enrol(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    async with services.db.session() as s:
        await s.execute(update(User).values(is_platform_admin=True))
    response = await http.get("/orglar")
    assert response.status_code == 303
    assert response.headers["location"] == "/hesap?ok=admin_totp"
